//! Headless UI tests: the REAL interface driven through AbstractTUI's
//! capture harness — same pipeline as production, no pty.
//!
//! The worker thread is replaced by a dummy command channel; gateway
//! payloads are fixtures applied to the store between frames exactly as
//! posted closures would apply them. No test touches the network.
//! Coverage honesty: the config-editing forms (profiles, routes, users,
//! tool policy, entity state) are pinned here; the remaining
//! entity-manage sub-forms and the runtimes/reservations actions are
//! proven live by scripts/pty_smoke.py rather than headless.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions, ScreensStore};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{
    entities_from_payload, host_state_from_payload, runtimes_from_payload, users_from_payload,
    AvailabilityData, ConnPhase, HostStateData, Identity, JournalEntry, Loadable, ProfilesData,
    ProvidersData, RoutesData, SandboxOutcome, Store,
};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

struct Harness {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
    /// Worker-command sender — health::settle takes it so tests drive
    /// the authority's retry lane through the same channel.
    tx: mpsc::Sender<Cmd>,
    /// The Ctx prober slot: tests install a recorder here.
    prober: ui::ProberSlot,
    /// The shared Models/Engines screens' backend: AbstractCore's
    /// contract fixtures + a call log (the gateway routes are pinned
    /// separately, in tests/http_transport.rs).
    mock: Arc<MockTransport>,
    screens: ScreensStore,
}

fn harness() -> Harness {
    harness_sized(Size::new(110, 34))
}

fn harness_sized(size: Size) -> Harness {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let tx_keep = tx.clone();
    let prober_slot: ui::ProberSlot = Rc::new(RefCell::new(None));
    let prober_keep = prober_slot.clone();
    let store_slot: Rc<RefCell<Option<Store>>> = Rc::new(RefCell::new(None));
    let store_out = store_slot.clone();
    let ui_slot: Rc<RefCell<Option<UiState>>> = Rc::new(RefCell::new(None));
    let ui_out = ui_slot.clone();
    let mock = Arc::new(MockTransport::default());
    let mock_mount = mock.clone();
    let screens_slot: Rc<RefCell<Option<ScreensStore>>> = Rc::new(RefCell::new(None));
    let screens_out = screens_slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        *store_out.borrow_mut() = Some(store);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        *ui_out.borrow_mut() = Some(ui_state);
        let transport: Arc<dyn ConsoleTransport> = mock_mount.clone();
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            ScreensOptions {
                // The same wiring as lib.rs: outcomes toast through the
                // gateway console's own notice lane.
                notice: Some(store.notice),
                // Fast polls: a job walks running → completed in a few
                // frames instead of seconds.
                poll_interval: Duration::from_millis(10),
                // Never a real browser from a test.
                opener: Some(Rc::new(|_url: &str| Ok(()))),
                ..ScreensOptions::default()
            },
        );
        *screens_out.borrow_mut() = Some(screens.store);
        let ctx = Ctx {
            tx: tx.clone(),
            overlays: overlays.clone(),
            quitter: quitter.clone(),
            store,
            ui: ui_state,
            modal: Rc::new(RefCell::new(None)),
            entity_drawer: Rc::new(RefCell::new(None)),
            env_token_set: false,
            prober: prober_slot.clone(),
            screens,
            screens_transport: transport,
        };
        ui::root(cx, ctx)
    })
    .expect("mount");
    let mut term = CaptureTerm::new(size);
    let cfg = RunConfig {
        probe: false,
        // Fixed capabilities: the host's TERM must not steer assertions.
        caps: Some(abstracttui::term::Capabilities::with(|c| {
            c.truecolor = true;
            c.colors_256 = true;
            c.unicode_ok = true;
        })),
        // These fixed caps leave osc52_copy false, which is exactly the
        // condition that arms the host-clipboard fallback. Refuse it: a
        // test that clicks "Copy to clipboard" would otherwise spawn
        // pbcopy and overwrite the clipboard of whoever ran the suite.
        platform_clipboard: false,
        ..RunConfig::default()
    };
    let driver = Driver::new(&mut app, &mut term, cfg).expect("driver");
    let store = store_slot.borrow().expect("store created");
    let ui = ui_slot.borrow().expect("ui state created");
    let screens = screens_slot.borrow().expect("screens created");
    Harness {
        app,
        term,
        driver,
        store,
        ui,
        rx,
        tx: tx_keep,
        prober: prober_keep,
        mock,
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

    /// Settle rule (round-4 P3-4): one turn = one render pass; effects
    /// triggered by a signal write run on the NEXT pass, and an effect
    /// that writes another signal needs one more. Hence the idiomatic
    /// `turns(2)` after a state change and `turns(3)` when a cascade
    /// (selection effect -> load -> render) must settle — not magic,
    /// just the effect-depth of the change under test.
    fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            last = self.turn();
        }
        last
    }

    /// Drain EVERY queued worker command (round-4 P3-4): negative
    /// asserts ("keep does not delete") should assert emptiness, not
    /// just absence-of-match — find_cmd silently discards non-matches.
    #[allow(dead_code)]
    fn drain_cmds(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }

    fn type_text(&mut self, text: &str) {
        self.term.push_input(text.as_bytes());
    }

    fn key(&mut self, bytes: &[u8]) {
        self.term.push_input(bytes);
    }

    fn press_escape(&mut self) {
        // Bare-ESC disambiguation: byte arrives, 30ms deadline, resolve.
        self.term.push_input(&[0x1b]);
        self.turn();
        std::thread::sleep(std::time::Duration::from_millis(45));
        self.turn();
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

    fn goto_screen(&mut self, n: usize) {
        self.ui.wizard.set(false);
        self.ui.screen.set(n);
        self.turns(2);
    }
}

fn admin_identity() -> Identity {
    Identity::from_me(&json!({
        "ok": true,
        "principal": {
            "user_id": "admin", "tenant_id": "default",
            "roles": ["admin", "user"], "admin": true
        },
        "auth": {"mode": "users"},
        "routing": {"mode": "per-principal"}
    }))
    .expect("identity parses")
}

// ---- fixtures (shape-faithful to the live gateway, values anonymized) --

fn profiles_fixture() -> ProfilesData {
    ProfilesData::from_value(&json!({
        "ok": true,
        "can_create_gateway_scope": true,
        "profiles": [
            {
                "id": "acme", "virtual_provider": "endpoint:acme",
                "display_name": "acme", "description": "test endpoint",
                "provider_family": "openai-compatible",
                "base_url": "http://127.0.0.1:9999/v1",
                "base_url_configured": true,
                "api_key_set": true, "api_key_fingerprint": "deadbeef1234",
                "scope": "gateway", "capabilities": ["text"],
                "allowed_models": [], "enabled": true
            },
            {
                "id": "openai", "provider_id": "openai",
                "display_name": "OpenAI", "description": "env key",
                "provider_family": "openai", "base_url": "",
                "default_base_url": "https://api.openai.com/v1",
                "base_url_configured": false,
                "api_key_set": true, "api_key_fingerprint": "cafecafe0000",
                "scope": "environment", "enabled": true,
                "managed": false, "synthetic": true, "source": "environment",
                "discovered_model_count": 0
            }
        ]
    }))
}

fn providers_fixture() -> ProvidersData {
    ProvidersData::from_value(&json!({
        "items": [
            {"name": "lmstudio", "display_name": "LMStudio", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []},
            {"name": "ollama", "display_name": "Ollama", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []},
            {"name": "endpoint:acme", "display_name": "acme", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []},
            // The live orphan class (2026-07-25 dump): an endpoint:*
            // discovery item with NO profile row behind it.
            {"name": "endpoint:unrelated", "display_name": "unrelated", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []}
        ],
        "default_provider": "lmstudio",
        "default_model": "test-model-a"
    }))
}

fn routes_fixture() -> RoutesData {
    RoutesData::from_value(&json!({
        "ok": true, "writable": true,
        "authority": "abstractcore.gateway_runtime",
        "source": "abstractcore.gateway_runtime",
        "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text",
             "label": "Text Input", "task": "text_understanding",
             "provider": "lmstudio", "model": "test-model-a",
             "reasoning": "medium",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.image", "kind": "input", "modality": "image",
             "label": "Image Input", "provider": "lmstudio", "model": "test-model-a",
             "source": "abstractcore.gateway_runtime", "configured": true,
             "covered_by": "input.text", "read_only": true},
            {"key": "input.video", "kind": "input", "modality": "video",
             "label": "Video Input", "provider": "lmstudio", "model": "test-model-a",
             "source": "abstractcore.gateway_runtime", "configured": true,
             "covered_by": "input.text", "read_only": false, "overrideable": true},
            {"key": "output.text", "kind": "output", "modality": "text",
             "label": "Text Output", "provider": "lmstudio", "model": "test-model-a",
             "source": "abstractcore.gateway_runtime", "configured": true,
             "derived_from": "input.text", "read_only": true},
            {"key": "output.voice", "kind": "output", "modality": "voice",
             "label": "Voice Output", "provider": "supertonic", "model": "supertonic-3",
             "options": {"voice": "M3"},
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "output.image.text_to_image", "kind": "output", "modality": "image",
             "label": "Image Generation", "task": "text_to_image",
             "provider": "mlx-gen", "model": "test-flux",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.sound", "kind": "input", "modality": "sound",
             "label": "Sound Input", "package_hint": "abstractsound",
             "source": "not_configured", "configured": false}
        ]
    }))
}

/// `GET /api/gateway/models/availability` for the fixture host: the
/// text route's weights are MISSING (the recommended 4-bit build), the
/// voice route's are here, the image route cannot be consulted, and an
/// unconfigured route reports `route not configured` — which is not a
/// missing download and must never be offered as one.
///
/// EVERY ROUTE HERE IS CONFIGURED, so `gaps` is empty even though the
/// recommended text build is absent: the operator routed `input.text` at
/// a model of their own and owes the starter kit nothing. That is the
/// host the banner used to warn at on every visit.
fn availability_fixture() -> AvailabilityData {
    AvailabilityData::from_value(&json!({
        "ok": true,
        "seeded": "recommended-v1",
        "routes": [
            {"key": "input.text", "provider": "lmstudio", "model": "test-model-a",
             "download_artifact": "qwen/qwen3.5-9b@4bit",
             "availability": {"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit",
                              "status": "absent", "downloadable": true,
                              "evidence": "lms ls --json",
                              "instruction": "lms get qwen/qwen3.5-9b@4bit"}},
            {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3",
             "availability": {"provider": "supertonic", "artifact": "supertonic-3",
                              "status": "installed", "downloadable": true,
                              "location": "/cache/supertonic-3"}},
            {"key": "output.image.text_to_image", "provider": "mlx-gen", "model": "test-flux",
             "availability": {"provider": "mlx-gen", "artifact": "test-flux",
                              "status": "unknown", "downloadable": false,
                              "evidence": "hf cache scan",
                              "instruction": "install huggingface_hub"}},
            {"key": "input.sound", "provider": "", "model": "",
             "availability": {"status": "unknown", "evidence": "route not configured"}}
        ],
        "recommended": {
            "total": 3, "installed": 2, "absent": 1, "unknown": 0,
            "would_download": [
                {"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "route": "input.text"}
            ],
            "gaps": [], "routes_unanswered": 0
        }
    }))
}

/// The same host BEFORE anyone configured it: `input.text` has nothing
/// routed to it and the recommended build is not on disk. This is the
/// one shape the weights banner exists for.
fn availability_fixture_fresh_install() -> AvailabilityData {
    AvailabilityData::from_value(&json!({
        "ok": true,
        "routes": [
            {"key": "input.text", "provider": "", "model": "",
             "availability": {"status": "unknown", "evidence": "route not configured"}}
        ],
        "recommended": {
            "total": 3, "installed": 2, "absent": 1, "unknown": 0,
            "would_download": [
                {"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "route": "input.text"}
            ],
            "gaps": [
                {"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "route": "input.text"}
            ],
            "routes_unanswered": 1
        }
    }))
}

/// `GET /host/state` for the fixture host — the REAL wire shape,
/// pinned from a live capture on the operator's Mac (2026-08-28):
///
/// * `device.allocated_bytes` is **0** while ~98 GB of weights are
///   resident. It is PROCESS-LOCAL on Metal; `host_in_use_bytes` /
///   `wired_limit_bytes` are the all-processes accelerator heap. This is
///   the bug the meter's rule exists to kill, so the fixture carries it.
/// * a sweep row (LM Studio loaded it, not the gateway) with NO measured
///   size — only `est_weights_bytes` — plus `cache_bytes`, and the
///   wire's own `source: "provider_server"` + `lockable: true` (lock
///   ADOPTS it, and the ADOPT wording keys off the SOURCE: `lockable` is
///   stamped `true` by the sweep and so can never select it).
/// * a DEGRADED gpu section with its reason, a locked resident model, a
///   row whose residency is UNKNOWN (`resident: null` — the tri-state's
///   third answer), and one session prompt cache.
fn host_state_fixture() -> HostStateData {
    host_state_from_payload(&json!({
        "ok": true, "ts": 1756252800.0,
        "host": {"host_id": "h-1", "host_name": "studio.local"},
        "memory": {
            "ram": {"total_bytes": 137438953472u64, "available_bytes": 51539607552u64,
                    "used_bytes": 85899345920u64, "percent": 62.5},
            "process": {"rss_bytes": 1073741824u64},
            "device": {"backend": "metal", "allocated_bytes": 0u64,
                       "total_bytes": 137438953472u64, "free_bytes": 31694962688u64,
                       "host_in_use_bytes": 105743990784u64,
                       "wired_limit_bytes": 115343360000u64}
        },
        "gpu": {"supported": false},
        "models": [
            {"runtime_id": "local:text_generation:mlx:qwen3-32b",
             "task": "text_generation", "provider": "mlx", "model": "qwen3-32b",
             "source": "local", "resident": true, "state": "provider_loaded",
             "pinned": true, "default": true,
             "locked": true, "lockable": true,
             "modalities": ["input.text", "output.text"],
             "size_bytes": 2147483648u64, "context_length": 8192u64,
             "calibrated_context_length": 8192u64, "context_calibrated": true,
             "host_id": "h-1", "host_name": "studio.local",
             "loaded_at": "2026-08-27T00:00:00Z", "last_used_at": "2026-08-27T00:05:00Z"},
            // The tri-state's third answer: the runtime holds a lease it
            // cannot confirm with the provider — resident is UNKNOWN.
            {"runtime_id": "local:unknown:lmstudio:mystery-model",
             "task": null, "provider": "lmstudio", "model": "mystery-model",
             "resident": null, "state": null},
            // The sweep row: LM Studio holds it, the gateway did not load
            // it. No measured size — only an ESTIMATE — and the wire's
            // `source: "provider_server"` / `lockable: true`, which lock
            // answers by ADOPTING the model.
            {"runtime_id": null, "task": "text_generation", "provider": "lmstudio",
             "model": "glm-4.6-gguf", "source": "provider_server", "resident": true,
             "state": "provider_loaded", "locked": false, "lockable": true,
             "size_bytes": null, "size_vram_bytes": null,
             "est_weights_bytes": 99857989632u64, "cache_bytes": 2147483648u64,
             "context_length": 131072u64, "default": false}
        ],
        "session_caches": [
            {"key": "agw.pc.v1.s-sess1:session", "provider": "mlx", "model": "qwen3-32b",
             "session_id": "sess1", "bytes": 4096u64, "token_count": 100u64}
        ],
        "totals": {"models": 3, "models_resident": 2, "model_bytes": 2147483648u64,
                   "cache_bytes_models": 2147483648u64,
                   "session_caches": 1, "session_cache_bytes": 4096u64},
        "degraded": ["gpu"],
        "reasons": {"gpu": "no GPU probe on this host"}
    }))
}

fn users_fixture() -> serde_json::Value {
    json!({
        "users": [
            {"user_id": "admin", "tenant_id": "default", "email": "",
             "roles": ["admin", "user"], "scopes": [], "enabled": true,
             "runtime_id": "default", "created_at": "2026-05-30T05:17:35Z",
             "updated_at": "2026-07-15T16:38:57Z"},
            {"user_id": "alice", "tenant_id": "default", "email": "a@x.io",
             "roles": ["user"], "scopes": [], "enabled": true,
             "runtime_id": "alice", "created_at": "2026-07-01T00:00:00Z",
             "updated_at": "2026-07-01T00:00:00Z"},
            {"user_id": "castorp", "tenant_id": "default", "email": "",
             "roles": ["entity"], "scopes": ["entity:castorp"], "enabled": true,
             "runtime_id": "castorp", "created_at": "2026-07-24T00:00:00Z",
             "updated_at": "2026-07-24T00:00:00Z"},
            {"user_id": "hypnosp", "tenant_id": "default", "email": "",
             "roles": ["entity"], "scopes": ["entity:hypnosp"], "enabled": true,
             "runtime_id": "hypnosp", "created_at": "2026-07-24T00:00:00Z",
             "updated_at": "2026-07-24T00:00:00Z"}
        ]
    })
}

fn entities_fixture() -> serde_json::Value {
    json!({
        "entities": [
            {"format_version": 1, "entity_id": "entity:testor", "name": "Testor",
             "slug": "testor", "home_id": "home-00000000",
             "handle": "testor@127.0.0.1",
             "state": {"state": "asleep", "written_by": "operator", "liveness": "alive"},
             "drives": {
                 "questions": {"open": 3, "resolved": 1, "ratio": 0.25},
                 "problems": {"open": 0, "repaired": 0, "ratio": null},
                 "interests": {"open": 7, "explored": 2, "ratio": 0.22}
             }}
        ]
    })
}

fn runtimes_fixture() -> serde_json::Value {
    json!({
        "runtimes": [
            {"kind": "default", "tenant_id": "default", "runtime_id": "default",
             "label": "Gateway default runtime",
             "owners": [{"user_id": "admin", "roles": ["admin"], "enabled": true}],
             "data_dir": "/tmp/runtime", "size_bytes": 8418852185u64},
            {"kind": "entity", "tenant_id": "default", "runtime_id": "runtime_testor",
             "label": "Testor", "entity": "testor",
             "owners": [{"user_id": "testor", "roles": ["entity"], "enabled": true}],
             "state": "asleep", "liveness": "alive",
             "data_dir": "/tmp/runtime/entities/testor", "size_bytes": 33487609u64}
        ]
    })
}

// =======================================================================
// Connection step
// =======================================================================

#[test]
fn boots_to_connection_wizard_step() {
    let mut h = harness();
    let screen = h.turn();
    assert!(
        screen.contains("AbstractGateway Console"),
        "header:\n{screen}"
    );
    // PageHost bar: numbered titles, engine-drawn active underline.
    assert!(screen.contains("1 Connection"), "page bar:\n{screen}");
    // 6 Workflows sits between Runtimes and Review; the bar's tail moved.
    assert!(screen.contains("6 Workflows"), "page bar:\n{screen}");
    assert!(
        screen.contains("7 Review & Test"),
        "page bar tail:\n{screen}"
    );
    assert!(screen.contains("Gateway URL"), "url field:\n{screen}");
    assert!(screen.contains("Admin token"), "token field:\n{screen}");
    assert!(
        screen.contains("not connected"),
        "honest initial state:\n{screen}"
    );
    assert!(screen.contains("next step"), "wizard hints:\n{screen}");
}

#[test]
fn pagehost_browse_navigation_digits_and_chords() {
    let mut h = harness();
    h.connect_as_admin();
    // Screen 1 (Providers): the table holds focus — digits reach the
    // shortcut surface. (On screen 0 the URL input rightly EATS digits,
    // the same property the old loop had — pinned by the brief.)
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    // Browse digits ride PageHost's number_jump: 4 jumps straight to
    // Users & Entities through the id bridge…
    h.type_text("4");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 3, "digit 4 → screen index 3");
    // …and Ctrl+N advances by EXACTLY one (the host's capture chord
    // consumes the key before the root wizard_next fallback — a double
    // advance here would mean both fired).
    h.key(b"\x0e");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 4, "Ctrl+N advances one step");
    // Ctrl+P walks back one.
    h.key(b"\x10");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 3, "Ctrl+P retreats one step");
    // Wizard mode disarms the free-navigation surface: digits refuse
    // with the reason instead of jumping.
    h.ui.wizard.set(true);
    h.turns(2);
    h.type_text("2");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 3, "wizard digit does not jump");
    assert!(
        h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .contains("digit jumps work in browse mode"),
        "wizard digit refusal carries its reason"
    );
}

#[test]
fn probe_normalizes_url_and_sends_connect() {
    let mut h = harness();
    h.turn();
    h.ui.conn_url.set("127.0.0.1:9999".into());
    h.ui.conn_token.set("tok-abc".into());
    h.turn();
    // The URL input is autofocused; Enter submits → connect_now.
    h.type_text("\r");
    h.turn();
    let cmd = h.find_cmd(|c| matches!(c, Cmd::Connect { .. }));
    match cmd {
        Some(Cmd::Connect { url, token }) => {
            assert_eq!(url, "http://127.0.0.1:9999", "scheme prepended");
            assert_eq!(token.0, "tok-abc");
        }
        other => panic!("expected Connect, got {other:?}"),
    }
    // URL field re-rendered with the normalized value.
    let screen = h.turn();
    assert!(
        screen.contains("http://127.0.0.1:9999"),
        "normalized:\n{screen}"
    );
}

#[test]
fn token_input_is_masked_on_screen() {
    let mut h = harness();
    h.turn();
    // Tab from the autofocused URL input to the token input, then type.
    h.key(b"\t");
    h.turn();
    h.type_text("hunter2secret");
    let screen = h.turns(2);
    assert!(
        !screen.contains("hunter2secret"),
        "the token must never appear in cells:\n{screen}"
    );
    assert!(screen.contains("•"), "bullets render instead:\n{screen}");
}

#[test]
fn connection_states_render_distinctly() {
    let mut h = harness();
    h.turn();
    h.store.conn.set(ConnPhase::Probing);
    let s = h.turn();
    assert!(s.contains("probing"), "probing:\n{s}");

    h.store
        .conn
        .set(ConnPhase::Unauthorized("bad token".into()));
    let s = h.turn();
    assert!(s.contains("unauthorized (401)"), "401 state:\n{s}");
    assert!(s.contains("bad token"), "verbatim detail:\n{s}");
    assert!(s.contains("rejected that token"), "actionable hint:\n{s}");

    h.store
        .conn
        .set(ConnPhase::Unreachable("connection refused".into()));
    let s = h.turn();
    assert!(s.contains("gateway unreachable"), "unreachable state:\n{s}");
    assert!(s.contains("connection refused"), "transport detail:\n{s}");

    h.connect_as_admin();
    let s = h.turn();
    assert!(s.contains("connected"), "connected:\n{s}");
    assert!(s.contains("admin"), "admin badge:\n{s}");
    assert!(s.contains("auth: users"), "auth mode badge:\n{s}");
    assert!(s.contains("routing: per-principal"), "routing badge:\n{s}");
}

#[test]
fn wizard_gate_blocks_next_until_connected_or_offline() {
    let mut h = harness();
    h.turn();
    // Ctrl+N while unconnected → the gate prompt, not a step change.
    // (']' is deliberately consumed by the focused URL input.)
    h.key(b"\x0e");
    let s = h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 0, "still on connection");
    assert!(s.contains("not connected"), "gate prompt visible:\n{s}");
    assert!(s.contains("Continue offline"), "offline option:\n{s}");
    // Commit the highlighted first option ("stay").
    h.type_text("\r");
    let _ = h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 0, "stay keeps the step");

    // Once connected, Ctrl+N advances.
    h.connect_as_admin();
    h.key(b"\x0e");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 1, "advanced to providers");
}

#[test]
fn wizard_gate_offline_choice_advances() {
    let mut h = harness();
    h.turn();
    h.key(b"\x0e");
    h.turns(2);
    // Down to "Continue offline", Enter.
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 1, "offline choice advances");
    let s = h.turn();
    assert!(
        s.contains("not loaded yet"),
        "providers screen shows honest not-asked state offline:\n{s}"
    );
}

// =======================================================================
// Providers step
// =======================================================================

/// The ONE unified list (operator ruling 2026-07-25: no second table).
/// Rows come from the profiles payload alone — managed AND synthetic —
/// under the join-law provider names; discovery demotes to the default
/// line + the not-configured-yet line. Nothing double-lists.
#[test]
fn profiles_table_renders_without_secrets() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    let s = h.turns(2);
    // Join-law provider names in the first column: managed rows show
    // their virtual endpoint:<id>; synthetic rows their bare id.
    assert!(s.contains("endpoint:acme"), "managed provider name:\n{s}");
    assert!(s.contains("openai"), "synthetic provider name:\n{s}");
    assert!(
        s.contains("stored (deadbee"),
        "key state shows fingerprint:\n{s}"
    );
    assert!(
        s.contains("env"),
        "synthetic origin says where it comes from:\n{s}"
    );
    // The second table is GONE; discovery-only names live in ONE line.
    assert!(
        !s.contains("Discovered providers"),
        "no second list on this screen:\n{s}"
    );
    let unconfigured = s
        .lines()
        .find(|l| l.contains("not configured yet"))
        .expect("the not-configured line renders");
    assert!(
        unconfigured.contains("lmstudio")
            && unconfigured.contains("ollama")
            && unconfigured.contains("endpoint:unrelated"),
        "discovery-only names (incl. the live orphan class):\n{unconfigured}"
    );
    assert!(
        !unconfigured.contains("acme") && !unconfigured.contains("openai"),
        "configured rows never double-list:\n{unconfigured}"
    );
    assert!(
        s.contains("gateway default: lmstudio / test-model-a"),
        "default line:\n{s}"
    );
    // The selected row (acme, managed) names its own verbs — the
    // web's per-row buttons as a TUI action line.
    assert!(
        s.contains("managed (gateway scope) · e edit · d delete"),
        "selection action line:\n{s}"
    );
}

/// The join law drives the row ACTIONS (live-verified 2026-07-25:
/// `endpoint:anthropic` is "Unknown provider" to the gateway while
/// bare `anthropic` serves models) — m on a synthetic row must ask
/// for the BARE name, on a managed row for endpoint:<id>.
#[test]
fn models_drilldown_uses_join_law_provider_names() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    // Row 0 = acme (managed).
    h.type_text("m");
    h.turns(2);
    assert!(
        matches!(
            h.find_cmd(|c| matches!(c, Cmd::LoadModels { .. })),
            Some(Cmd::LoadModels { provider }) if provider == "endpoint:acme"
        ),
        "managed row asks for its virtual provider"
    );
    h.press_escape();
    h.turns(2);
    // Row 1 = openai (synthetic, provider_id "openai"). Selection set
    // directly — focus restoration after a modal close is not what
    // this test pins.
    h.ui.profile_sel.set(1);
    h.turns(2);
    h.type_text("m");
    h.turns(2);
    assert!(
        matches!(
            h.find_cmd(|c| matches!(c, Cmd::LoadModels { .. })),
            Some(Cmd::LoadModels { provider }) if provider == "openai"
        ),
        "synthetic row asks for the BARE provider id, never endpoint:openai"
    );
}

#[test]
fn add_profile_form_validates_and_sends_create() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);

    h.type_text("a");
    let s = h.turns(2);
    assert!(s.contains("Add a provider connection"), "form open:\n{s}");
    assert!(s.contains("choose a family…"), "placeholder family:\n{s}");

    // The id input is autofocused: type an id, then walk to Save and
    // press it with the family still on the placeholder.
    h.type_text("acme2");
    h.turn();
    // Happy path (P1-C disclosure): id → family → base URL → API key →
    // More-options header → Test → Save (advanced fields folded away).
    for _ in 0..6 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    let s = h.turns(2);
    assert!(
        s.contains("choose a provider family"),
        "validation error inline, modal stays:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. }))
            .is_none(),
        "no write leaves the app on a validation failure"
    );

    // Fix the family: Shift+Tab from Save back to the Select (family is
    // index 1, Save index 6 in the folded happy path → 5 stops).
    for _ in 0..5 {
        h.key(b"\x1b[Z");
        h.turn();
    }
    h.type_text("\r"); // open the popup
    h.turns(2);
    h.key(b"\x1b[B"); // down to "openai-compatible"
    h.turn();
    h.type_text("\r"); // commit
    h.turns(2);

    // Base URL, then API key.
    h.key(b"\t");
    h.turn();
    h.type_text("http://127.0.0.1:1234/v1");
    h.turn();
    h.key(b"\t");
    h.turn();
    h.type_text("sk-testkey");
    h.turn();
    // API key → More-options header → Test → Save (advanced fields are
    // folded; scope defaults to gateway for admin, allowed stays []).
    for _ in 0..3 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);

    let cmd = h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. }));
    match cmd {
        Some(Cmd::SaveProfile {
            create, id, body, ..
        }) => {
            assert!(create);
            assert_eq!(id, "acme2");
            assert_eq!(body["provider_family"], "openai-compatible");
            assert_eq!(body["base_url"], "http://127.0.0.1:1234/v1");
            assert_eq!(body["api_key"], "sk-testkey");
            assert_eq!(body["scope"], "gateway");
            // Empty allowlist field → explicit [] (live discovery); the
            // array must ride every save or clearing is impossible.
            assert_eq!(body["allowed_models"], serde_json::json!([]));
        }
        other => panic!("expected SaveProfile, got {other:?}"),
    }
}

#[test]
fn edit_profile_never_echoes_stored_key_and_closes_on_success() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);

    // Row 0 = acme (managed, key stored). Open edit.
    h.type_text("e");
    let s = h.turns(2);
    assert!(s.contains("Edit profile 'acme'"), "edit form:\n{s}");
    assert!(
        s.contains("a key is stored (deadbeef1234)"),
        "stored-key note:\n{s}"
    );
    assert!(
        s.contains("clear the stored key on save"),
        "clear affordance:\n{s}"
    );
    // The form must NOT prefill any secret — the API never returns one,
    // and the field starts empty (bullets would render if it did).
    assert!(!s.contains("sk-"), "no secret text anywhere:\n{s}");

    // Simulate the worker completing the write for this form: the modal
    // closes on success. (form_id is 1-based per process; fetch it from
    // the command the form sends.)
    // Edit mode: id is static, family autofocuses, the disclosure is
    // OPEN. family → base URL → API key → More-options header →
    // display → description → allowed → scope → clear-key → enabled →
    // Test → Save (11 tabs from family).
    for _ in 0..11 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);
    let cmd = h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. }));
    let form_id = match cmd {
        Some(Cmd::SaveProfile {
            create,
            id,
            body,
            form_id,
        }) => {
            assert!(!create);
            assert_eq!(id, "acme");
            assert!(
                body.get("api_key").is_none(),
                "blank key field must OMIT api_key (keep stored): {body:?}"
            );
            // Scope now rides edits too (web parity: the gateway moves
            // the profile between stores when scope changes).
            assert_eq!(body["scope"], "gateway", "unchanged scope resent as-is");
            form_id.expect("edit form correlates its write")
        }
        other => panic!("expected SaveProfile, got {other:?}"),
    };
    h.ui.write_done.set(Some((form_id, Ok("applied".into()))));
    let s = h.turns(3);
    assert!(
        !s.contains("Edit profile 'acme'"),
        "modal closed on success:\n{s}"
    );

    // And on failure the modal stays with the verbatim error.
    h.type_text("e");
    h.turns(2);
    for _ in 0..11 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);
    let form_id = match h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. })) {
        Some(Cmd::SaveProfile { form_id, .. }) => form_id.unwrap(),
        other => panic!("expected SaveProfile, got {other:?}"),
    };
    h.ui.write_done.set(Some((
        form_id,
        Err("HTTP 400: base_url must include a scheme".into()),
    )));
    let s = h.turns(3);
    assert!(
        s.contains("Edit profile 'acme'"),
        "modal stays on failure:\n{s}"
    );
    assert!(
        s.contains("base_url must include a scheme"),
        "gateway detail verbatim:\n{s}"
    );
}

/// Web parity: `e` on a synthetic row is OVERRIDE — the create form
/// prefilled with the row's identity (same id → the managed copy
/// shadows the env/core row server-side). Delete still refuses with
/// the teach-the-override reason.
#[test]
fn synthetic_row_override_opens_prefilled_create() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    // Move selection to row 1 (openai, synthetic).
    h.key(b"\x1b[B");
    h.turns(2);
    h.type_text("e");
    let s = h.turns(2);
    assert!(
        s.contains("Override 'openai' — create a managed connection"),
        "override form (not a refusal, not Edit):\n{s}"
    );
    assert!(
        s.contains("already usable from environment config"),
        "the override banner says why this form exists:\n{s}"
    );
    // Prefilled but untouched → NOT dirty → one Esc closes.
    h.press_escape();
    let s = h.turns(2);
    assert!(
        !s.contains("Override 'openai'"),
        "untouched override form closes on one Esc:\n{s}"
    );
    h.type_text("d");
    let s = h.turns(2);
    assert!(
        s.contains("only managed profiles delete here"),
        "delete refusal teaches the override path:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::DeleteProfile { .. }))
            .is_none(),
        "no delete leaves the app for a synthetic row"
    );
}

/// Saving an untouched override posts a CREATE carrying the synthetic
/// row's identity — id, family — so the managed profile lands under
/// the exact name that shadows the env/core row.
#[test]
fn override_save_posts_create_with_prefilled_identity() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.key(b"\x1b[B");
    h.turns(2);
    h.type_text("e");
    h.turns(2);
    // Create-mode fold: id (autofocused) → family → base URL →
    // API key → More-options header → Test → Save.
    for _ in 0..6 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. })) {
        Some(Cmd::SaveProfile {
            create, id, body, ..
        }) => {
            assert!(create, "override is a CREATE (POST), never a PUT");
            assert_eq!(id, "openai", "the synthetic row's bare id rides");
            assert_eq!(body["provider_family"], "openai", "family preselected");
            assert!(
                body.get("api_key").is_none(),
                "no key typed → none sent (the env key stays where it is): {body:?}"
            );
        }
        other => panic!("expected SaveProfile, got {other:?}"),
    }
}

#[test]
fn escape_closes_form_modal() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("a");
    let s = h.turns(2);
    assert!(s.contains("Add a provider connection"), "form open:\n{s}");
    h.press_escape();
    let s = h.turns(2);
    assert!(
        !s.contains("Add a provider connection"),
        "Esc closes the form:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SaveProfile { .. }))
            .is_none(),
        "closing writes nothing"
    );
}

#[test]
fn delete_profile_needs_danger_confirm() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("d");
    let s = h.turns(2);
    assert!(
        s.contains("Delete provider connection 'acme'"),
        "confirm:\n{s}"
    );
    // Initial highlight is "keep" — Enter must NOT delete.
    h.type_text("\r");
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::DeleteProfile { .. }))
            .is_none(),
        "keep does not delete"
    );
    // Again, choose the danger option explicitly.
    h.type_text("d");
    h.turns(2);
    h.key(b"\x1b[A"); // up to "Delete the profile"
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::DeleteProfile { .. })) {
        Some(Cmd::DeleteProfile { id }) => assert_eq!(id, "acme"),
        other => panic!("expected DeleteProfile, got {other:?}"),
    }
}

// =======================================================================
// Routes step
// =======================================================================

#[test]
fn routes_table_renders_states_distinctly() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    let s = h.turns(2);
    // THE SHARED STATE VOCABULARY — the same four strings the
    // AbstractCore console's routes table prints for these states.
    assert!(s.contains("configured"), "configured state:\n{s}");
    assert!(s.contains("covered by input.text"), "covered state:\n{s}");
    assert!(s.contains("derived ← input.text"), "derived state:\n{s}");
    assert!(
        s.contains("not configured"),
        "an unconfigured row says so — 'default' read as 'a default is \
         set' on a screen literally about defaults:\n{s}"
    );
    assert!(s.contains("writable"), "authority banner:\n{s}");
    assert!(s.contains("supertonic-3"), "route model cell:\n{s}");
    // No double-width emoji in a table cell: the padlock measures 2
    // cells and terminals draw it at their own advance, sliding every
    // column to its right (the sibling console's glyph research).
    assert!(
        !s.contains('\u{1F512}') && s.contains('\u{2298}'),
        "the locked marker is U+2298, not a padlock emoji:\n{s}"
    );
}

#[test]
fn routes_body_errors_render_as_failure() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store
        .routes
        .set(Loadable::Ready(RoutesData::from_value(&json!({
            "ok": false, "writable": false,
            "authority": "abstractcore.server",
            "source": "abstractcore.server",
            "errors": ["remote core unreachable: connection refused"],
            "routes": [
                {"key": "input.text", "kind": "input", "modality": "text",
                 "label": "Text Input", "source": "not_configured", "configured": false}
            ]
        }))));
    let s = h.turns(2);
    assert!(
        s.contains("read-only"),
        "writable:false renders read-only banner:\n{s}"
    );
    assert!(
        s.contains("remote core unreachable"),
        "ok:false errors verbatim (body over transport):\n{s}"
    );
}

#[test]
fn route_editor_default_mode_and_applies_now() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    // Select input.sound (row 6, unconfigured) and open the editor.
    for _ in 0..6 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("\r");
    let s = h.turns(2);
    assert!(s.contains("Route — Sound Input"), "editor open:\n{s}");
    assert!(
        s.contains("nothing configured — engine decides"),
        "honest applies-now:\n{s}"
    );
    assert!(
        s.contains("pickers disabled — the engine resolves this route"),
        "default mode disables pickers:\n{s}"
    );
}

#[test]
fn route_editor_override_flow_sends_put_with_picked_pair() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    for _ in 0..6 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);

    // Mode radio has focus first: Down switches to override.
    h.key(b"\x1b[B");
    let s = h.turns(2);
    assert!(
        s.contains("choose a provider…"),
        "placeholder provider:\n{s}"
    );
    assert!(s.contains("choose a provider first"), "model gated:\n{s}");

    // Tab to the provider select, open, pick lmstudio (first option
    // after the placeholder).
    h.key(b"\t");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r");
    let s = h.turns(2);
    assert!(s.contains("discovering models…"), "models loading:\n{s}");
    assert!(
        matches!(
            h.find_cmd(|c| matches!(c, Cmd::LoadModels { .. })),
            Some(Cmd::LoadModels { provider }) if provider == "lmstudio"
        ),
        "models requested for the picked provider"
    );

    // Deliver the model list as the worker would.
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into(), "test-model-b".into()]),
        );
    });
    let s = h.turns(2);
    assert!(s.contains("choose a model…"), "model placeholder:\n{s}");

    // Tab to the model combobox, open, pick test-model-b.
    h.key(b"\t");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    h.key(b"\x1b[B");
    h.turn();
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r");
    h.turns(2);

    // Tab past base URL + options to Save.
    for _ in 0..3 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);

    match h.find_cmd(|c| matches!(c, Cmd::PutRoute { .. })) {
        Some(Cmd::PutRoute {
            kind,
            modality,
            task,
            body,
            key,
            ..
        }) => {
            assert_eq!(kind, "input");
            assert_eq!(modality, "sound");
            assert_eq!(task, None);
            assert_eq!(key, "input.sound");
            assert_eq!(body["provider"], "lmstudio");
            assert_eq!(body["model"], "test-model-b");
        }
        other => panic!("expected PutRoute, got {other:?}"),
    }
}

/// The reasoning effort is a property of TEXT GENERATION, and it lives
/// on Core's route row — both entry points read and write the ONE
/// stored value. The control belongs on the text route and on no other,
/// exactly as the web console's `isTextGenerationDefault` gates it.
///
/// The save also proves the field-preserving contract from the client
/// side: every field this form OWNS is sent explicitly, `""`/`{}`
/// included, so a base URL or an options dict the operator emptied is
/// actually cleared rather than silently restored by the merge.
#[test]
fn text_route_editor_carries_reasoning_and_sends_owned_fields_explicitly() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into(), "test-model-b".into()]),
        );
    });
    h.turns(2);
    // Row 0 = input.text, configured with reasoning "medium".
    h.type_text("e");
    let s = h.turns(3);
    assert!(
        s.contains("Route — Text Input (input.text)"),
        "editor:\n{s}"
    );
    assert!(
        s.contains("Applies now: lmstudio / test-model-a"),
        "the stored pair leads the form:\n{s}"
    );
    assert!(
        s.contains("reasoning medium"),
        "Applies now names the stored effort:\n{s}"
    );
    assert!(s.contains("reasoning"), "the reasoning row renders:\n{s}");

    // Tab: mode → provider → model → base URL → reasoning → options → MTP →
    // [Save]. Nothing is edited: the pair the row already carries is
    // re-sent (this form's mode radio requires it), and every other
    // owned field goes out explicitly.
    for _ in 0..7 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);

    match h.find_cmd(|c| matches!(c, Cmd::PutRoute { .. })) {
        Some(Cmd::PutRoute { body, key, .. }) => {
            assert_eq!(key, "input.text");
            assert_eq!(body["provider"], "lmstudio");
            assert_eq!(body["model"], "test-model-a");
            assert_eq!(
                body["reasoning"], "medium",
                "the stored effort survives a save that did not touch it"
            );
            assert_eq!(
                body["base_url"], "",
                "an empty owned field is sent EMPTY — omitting it would let \
                 the field-preserving merge restore a cleared value"
            );
            assert_eq!(body["options"], serde_json::json!({}), "same for options");
        }
        other => panic!("expected PutRoute, got {other:?}"),
    }
}

#[test]
fn text_route_audition_uses_the_mtp_control_and_preserves_explicit_off() {
    for value in [
        json!(false),
        json!({"mode":"native_mtp", "num_draft_tokens":4, "require_acceleration":false}),
    ] {
        let mut h = harness();
        h.connect_as_admin();
        h.goto_screen(2);
        let mut routes = routes_fixture();
        routes.rows[0].options = Some(json!({"speculation":value}));
        h.store.routes.set(Loadable::Ready(routes));
        h.store.providers.set(Loadable::Ready(providers_fixture()));
        h.store.models.update(|models| {
            models.insert(
                "lmstudio".into(),
                Loadable::Ready(vec!["test-model-a".into()]),
            );
        });
        h.turns(2);
        h.type_text("e");
        let screen = h.turns(3);
        assert!(
            screen.contains("MTP default"),
            "MTP policy control must be visible: {screen}"
        );
        // mode -> provider -> model -> URL -> reasoning -> options -> MTP -> Save -> Test
        for _ in 0..8 {
            h.key(b"\t");
            h.turn();
        }
        h.type_text("\r");
        h.turns(2);
        match h.find_cmd(|command| matches!(command, Cmd::TestRoute { .. })) {
            Some(Cmd::TestRoute { controls, .. }) => {
                assert_eq!(controls["reasoning"], "medium");
                if value == json!(false) {
                    assert_eq!(controls["speculation"], false);
                } else {
                    assert_eq!(controls["speculation"]["num_draft_tokens"], 4);
                    assert_eq!(controls["speculation"]["require_acceleration"], true);
                }
            }
            other => panic!("expected MTP audition command, got {other:?}"),
        }
    }
}

/// The reasoning control exists nowhere else — a voice route's editor
/// must not offer an effort the store would ignore.
#[test]
fn reasoning_row_appears_only_on_the_text_route() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    for _ in 0..4 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("e"); // output.voice
    let s = h.turns(3);
    assert!(
        s.contains("Route — Voice Output (output.voice)"),
        "editor:\n{s}"
    );
    assert!(
        !s.contains("reasoning"),
        "no reasoning control on a non-text route:\n{s}"
    );
}

#[test]
fn route_editor_task_key_derivation() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.turns(2);
    // Row 5 = output.image.text_to_image (configured). Clear it via x.
    for _ in 0..5 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("x");
    let s = h.turns(2);
    assert!(
        s.contains("Clear the override on output.image.text_to_image"),
        "confirm:\n{s}"
    );
    h.key(b"\x1b[A"); // up to the danger option
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::ClearRoute { .. })) {
        Some(Cmd::ClearRoute {
            kind,
            modality,
            task,
            key,
        }) => {
            assert_eq!(kind, "output");
            assert_eq!(modality, "image");
            assert_eq!(task.as_deref(), Some("text_to_image"), "task from the key");
            assert_eq!(key, "output.image.text_to_image");
        }
        other => panic!("expected ClearRoute, got {other:?}"),
    }
}

#[test]
fn read_only_and_covered_rows_refuse_editing_with_reasons() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.turns(2);
    // Row 1: input.image (covered, read_only, NOT overrideable).
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("e");
    let s = h.turns(2);
    assert!(
        s.contains("covered and not overrideable"),
        "covered refusal:\n{s}"
    );
    // Row 3: output.text (derived).
    h.key(b"\x1b[B");
    h.turn();
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("e");
    let s = h.turns(2);
    assert!(
        s.contains("derives from input.text"),
        "derived refusal:\n{s}"
    );
    // x on a default row: nothing to clear.
    for _ in 0..3 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("x");
    let s = h.turns(2);
    assert!(
        s.contains("no explicit override to clear"),
        "clear refusal:\n{s}"
    );
}

// =======================================================================
// Users & entities step
// =======================================================================

#[test]
fn users_and_entities_render_with_admin_gate() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities_fixture())));
    let s = h.turns(2);
    assert!(s.contains("alice"), "user row:\n{s}");
    assert!(s.contains("admin, user"), "roles cell:\n{s}");
    // The partition (operator complaint 2026-07-25): entity principals
    // NEVER render as user rows — they are counted in the teaching
    // line and managed through the entities lane (rotate/delete cannot
    // target what is not rendered).
    assert!(
        !s.contains("castorp") && !s.contains("hypnosp"),
        "entity principals hidden from the users table:\n{s}"
    );
    assert!(
        s.contains("2 entities also hold their own access tokens"),
        "partition teaching line with count:\n{s}"
    );
    assert!(s.contains("Testor"), "entity row:\n{s}");
    assert!(s.contains("asleep"), "entity state:\n{s}");
    assert!(s.contains("q:3 p:0 i:7"), "drives summary:\n{s}");
    // Entities are manageable (state/config), while creation/summon
    // stay deliberately outside this console — the title teaches both.
    assert!(s.contains("m = manage"), "manage affordance:\n{s}");
    assert!(
        s.contains("stay outside this console"),
        "creation out-of-scope note:\n{s}"
    );
    // Selecting a row keeps its manage snapshot warm; the inline strip
    // became the right Drawer — `i` teaches and toggles it.
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadEntityDetail { .. }))
            .is_some(),
        "selection-driven detail load fired"
    );
    assert!(
        s.contains("i inspects the selected entity"),
        "drawer teaching line:\n{s}"
    );
    h.type_text("i");
    let s = h.turns(3);
    assert!(s.contains("Entity inspector"), "drawer open:\n{s}");
    // Deterministic here: no worker runs, so entity_detail is Loading
    // and the drawer prints its honest loading line. (F16: the old
    // `|| contains("Testor")` arm was always true via the roster —
    // the tautological-disjunction class this suite itself names.)
    assert!(
        s.contains("reading Testor"),
        "drawer shows the selected entity's loading state:\n{s}"
    );
    // i again closes (toggle) — the panel must not linger.
    h.type_text("i");
    let s = h.turns(3);
    assert!(
        !s.contains("Entity inspector"),
        "drawer closed on second i:\n{s}"
    );
}

#[test]
fn forbidden_users_render_admin_hint() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Failed(abstractgateway_console::api::ApiError {
            kind: abstractgateway_console::api::ApiErrorKind::Forbidden,
            message: "admin role required".into(),
            body: None,
            timed_out: false,
        }));
    let s = h.turns(2);
    assert!(s.contains("forbidden (403)"), "403 state:\n{s}");
    assert!(s.contains("admin role required"), "verbatim detail:\n{s}");
    assert!(s.contains("needs an admin token"), "hint:\n{s}");
}

#[test]
fn create_user_flow_and_token_shown_once() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.turns(2);
    h.type_text("a");
    let s = h.turns(2);
    assert!(s.contains("New gateway user"), "form:\n{s}");
    assert!(
        s.contains("shown once after create"),
        "token teaching:\n{s}"
    );

    h.type_text("bob");
    h.turn();
    // user id → email → roles → tenant → runtime → enabled → Create
    for _ in 0..6 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);
    let form_id = match h.find_cmd(|c| matches!(c, Cmd::CreateUser { .. })) {
        Some(Cmd::CreateUser { body, form_id }) => {
            assert_eq!(body["user_id"], "bob");
            assert_eq!(body["roles"], json!(["user"]));
            assert_eq!(body["enabled"], true);
            // Blank advanced fields are OMITTED — the gateway's own
            // defaults apply (never a fabricated tenant/runtime value).
            assert!(body.get("tenant_id").is_none(), "blank tenant omitted");
            assert!(body.get("runtime_id").is_none(), "blank runtime omitted");
            form_id.expect("create form correlates its write")
        }
        other => panic!("expected CreateUser, got {other:?}"),
    };

    // The real sequence: the worker reports success (form closes), THEN
    // the token arrives on the queue — the token modal must wait for
    // the form modal to close, never bulldoze it.
    h.ui.token_queue
        .update(|q| q.push(("bob".into(), "agw_once_only_XYZ".into())));
    let s = h.turns(2);
    assert!(
        s.contains("New gateway user"),
        "token modal WAITS while the form is still open:\n{s}"
    );
    assert!(
        !s.contains("agw_once_only_XYZ"),
        "token not shown over the form:\n{s}"
    );
    h.ui.write_done.set(Some((form_id, Ok("applied".into()))));
    let s = h.turns(3);
    assert!(s.contains("Access token for 'bob'"), "token modal:\n{s}");
    assert!(s.contains("agw_once_only_XYZ"), "token visible ONCE:\n{s}");
    assert!(s.contains("shown ONCE"), "the once warning:\n{s}");
}

#[test]
fn rotate_token_requires_explicit_danger_choice() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.turns(2);
    h.type_text("t");
    let s = h.turns(2);
    assert!(s.contains("Rotate the token for 'admin'"), "confirm:\n{s}");
    h.type_text("\r"); // initial = keep
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::PatchUser { .. })).is_none(),
        "keep sends nothing"
    );
    h.type_text("t");
    h.turns(2);
    h.key(b"\x1b[A");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::PatchUser { .. })) {
        Some(Cmd::PatchUser { user_id, body, .. }) => {
            assert_eq!(user_id, "admin");
            assert_eq!(body["rotate_token"], true);
        }
        other => panic!("expected PatchUser, got {other:?}"),
    }
}

#[test]
fn entity_manage_menu_state_flow_sends_post() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities_fixture())));
    h.turns(2);

    // m over the selected entity opens the manage menu.
    h.type_text("m");
    let s = h.turns(2);
    assert!(s.contains("Manage entity 'Testor'"), "manage menu:\n{s}");
    assert!(s.contains("wake / sleep / pause"), "state option:\n{s}");
    assert!(s.contains("Re-embed"), "reembed option:\n{s}");

    // Initial pick = state → Enter opens the state modal.
    h.type_text("\r");
    let s = h.turns(3);
    assert!(s.contains("Entity state — Testor"), "state modal:\n{s}");
    assert!(s.contains("dream pass"), "dream option:\n{s}");

    // Testor is asleep (fixture) → radio starts on asleep (ix 1). Move
    // down one to "asleep + dream pass", walk to Apply, press it.
    h.key(b"\x1b[B");
    h.turn();
    h.key(b"\t"); // → reason
    h.turn();
    h.type_text("nightly consolidation");
    h.turn();
    h.key(b"\t"); // → Apply
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::EntityState { .. })) {
        Some(Cmd::EntityState { name, body }) => {
            assert_eq!(name, "Testor");
            assert_eq!(body["state"], "asleep");
            assert_eq!(body["dream"], true);
            assert_eq!(body["reason"], "nightly consolidation");
        }
        other => panic!("expected EntityState, got {other:?}"),
    }
    // The modal closed on Apply (outcome rides toast + journal).
    let s = h.turns(2);
    assert!(!s.contains("Entity state — Testor"), "modal closed:\n{s}");
}

#[test]
fn entity_tool_policy_editor_saves_changed_phases_only() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities_fixture())));
    h.turns(2);

    // Open the manage menu, walk down to "Tool policy", Enter.
    h.type_text("m");
    h.turns(2);
    for _ in 0..5 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);

    // The editor opened with a Loading slot; feed the folded policy the
    // worker would post (visit has two tools, work has one).
    h.store.entity_policy.set(Loadable::Ready(
        abstractgateway_console::store::ToolPolicyData {
            entity: "Testor".into(),
            phases: vec![
                (
                    "visit".into(),
                    vec!["web_search".into(), "read_file".into()],
                    "default".into(),
                ),
                ("work".into(), vec!["web_search".into()], "custom".into()),
            ],
            all_tools: vec!["web_search".into(), "read_file".into(), "write_file".into()],
        },
    ));
    let s = h.turns(3);
    assert!(s.contains("Tool policy — Testor"), "editor open:\n{s}");
    assert!(s.contains("visit"), "phase rows:\n{s}");
    assert!(s.contains("(custom)"), "provenance shown:\n{s}");

    // No changes → Save refuses with the reason.
    // Focus: visit MultiSelect autofocus is not set; walk to Save.
    for _ in 0..3 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r");
    let s = h.turns(2);
    assert!(
        s.contains("no changes to save"),
        "unchanged save refused with reason:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SaveToolPolicy { .. }))
            .is_none(),
        "no write on a no-op save"
    );

    // Toggle one tool in the VISIT phase through the real MultiSelect
    // (Shift+Tab back: Save → work → visit), then Save — the write must
    // carry ONLY the changed phase (delta law, web parity).
    for _ in 0..2 {
        h.key(b"\x1b[Z");
        h.turn();
    }
    h.type_text("\r"); // open the visit popup
    h.turns(2);
    h.key(b"\x1b[B"); // highlight an option
    h.turn();
    h.type_text(" "); // toggle it (working copy)
    h.turn();
    h.type_text("\r"); // Enter COMMITS the set (Esc would discard it)
    h.turns(2);
    // Focus stays on the visit trigger: work → Save.
    for _ in 0..2 {
        h.key(b"\t");
        h.turn();
    }
    h.type_text("\r"); // Save grants
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SaveToolPolicy { .. })) {
        Some(Cmd::SaveToolPolicy { name, body, .. }) => {
            assert_eq!(name, "Testor");
            let policy = body["policy"].as_object().expect("policy object");
            assert!(
                policy.contains_key("visit"),
                "changed phase rides: {body:?}"
            );
            assert!(
                !policy.contains_key("work"),
                "UNCHANGED phase must not ride (delta law): {body:?}"
            );
        }
        other => panic!("expected SaveToolPolicy, got {other:?}"),
    }
}

// =======================================================================
// Runtimes + review
// =======================================================================

#[test]
fn runtimes_table_renders_sizes_and_states() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    let s = h.turns(2);
    assert!(
        s.contains("Gateway default run"),
        "default row (label cell may ellipsize):\n{s}"
    );
    assert!(s.contains("7.8 GiB"), "humanized size:\n{s}");
    assert!(s.contains("asleep (alive)"), "entity state cell:\n{s}");
}

#[test]
fn review_journal_renders_writes_and_verification() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(6);
    h.store.journal.update(|j| {
        j.push(JournalEntry {
            when: "10:00:00Z".into(),
            action: "PUT capability route output.voice".into(),
            outcome: Ok("applied".into()),
            verified: Some(Ok(
                "GET shows output.voice = supertonic / supertonic-3".into()
            )),
        });
        j.push(JournalEntry {
            when: "10:01:00Z".into(),
            action: "POST user 'bob'".into(),
            outcome: Err("HTTP 400: user exists".into()),
            verified: None,
        });
    });
    let s = h.turns(2);
    assert!(
        s.contains("PUT capability route output.voice"),
        "entry:\n{s}"
    );
    assert!(
        s.contains("verified · GET shows output.voice"),
        "verify line:\n{s}"
    );
    assert!(
        s.contains("HTTP 400: user exists"),
        "failure verbatim:\n{s}"
    );
    assert!(s.contains("no test run yet"), "sandbox empty state:\n{s}");
}

#[test]
fn review_empty_state_and_finish_button() {
    let mut h = harness();
    h.connect_as_admin();
    h.ui.screen.set(6); // stay in wizard mode
    let s = h.turns(2);
    assert!(
        s.contains("no changes applied this session"),
        "journal empty state:\n{s}"
    );
    assert!(
        s.contains("Finish — switch to browse mode"),
        "wizard finish:\n{s}"
    );
    // The redesign: the sandbox is INLINE — its pickers and prompt live
    // on the screen itself, no modal between the operator and the test.
    assert!(
        s.contains("provider") && s.contains("model") && s.contains("prompt"),
        "inline sandbox pickers + prompt render on the screen:\n{s}"
    );
}

/// The inline sandbox flow end-to-end (redesign 2026-07-25): prefill
/// by name (the Providers `t` contract), models load for the pick,
/// `g` fires exactly one SandboxTest with the picked pair, and the
/// outcome renders IN FULL — the modal used to ellipsize the paid-for
/// response at 90 chars.
#[test]
fn review_inline_sandbox_runs_and_renders_full_result() {
    let mut h = harness();
    h.connect_as_admin();
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    // The durable-name prefill: provider by name, model by name (the
    // index signals derive from these when the lists land).
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-b".into());
    h.goto_screen(6);
    h.turns(2);
    // Entering with a picked provider requests its models.
    let cmd = h.find_cmd(|c| matches!(c, Cmd::LoadModels { .. }));
    match cmd {
        Some(Cmd::LoadModels { provider }) => assert_eq!(provider, "lmstudio"),
        other => panic!("expected LoadModels for the picked provider, got {other:?}"),
    }
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into(), "test-model-b".into()]),
        );
    });
    h.turns(3);
    // Enter in the (autofocused) prompt runs the test with the picks.
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. })) {
        Some(Cmd::SandboxTest {
            provider,
            model,
            prompt,
        }) => {
            assert_eq!(provider, "lmstudio");
            assert_eq!(model, "test-model-b");
            assert!(
                prompt.contains("Reply with one short sentence"),
                "default prompt rides the test: {prompt}"
            );
        }
        other => panic!("expected SandboxTest, got {other:?}"),
    }
    let s = h.turns(2);
    assert!(
        s.contains("⟳ generating"),
        "synchronous Loading renders (double-press guard):\n{s}"
    );
    // A second Enter while running must NOT fire a second real
    // generation (a paid call).
    h.type_text("\r");
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. }))
            .is_none(),
        "no second generation while one runs"
    );
    // The outcome: a response well past the modal's old 90-char
    // ellipsize, with a tail marker that must survive to the screen.
    let long_response = format!(
        "{} WRAP-TAIL-SURVIVES",
        "the model answered with many words ".repeat(4)
    );
    h.store.sandbox.set(Loadable::Ready(SandboxOutcome {
        ok: true,
        error: None,
        response: long_response,
        routed_provider: Some("lmstudio".into()),
        profile: None,
        usage: Some("{\"total_tokens\":42}".into()),
        provider: "lmstudio".into(),
        model: "test-model-b".into(),
    }));
    let s = h.turns(2);
    assert!(
        s.contains("✓") && s.contains("lmstudio / test-model-b"),
        "outcome names its pair:\n{s}"
    );
    assert!(
        s.contains("routed via lmstudio"),
        "routing line renders:\n{s}"
    );
    assert!(
        s.contains("WRAP-TAIL-SURVIVES"),
        "the FULL response renders (no 90-char ellipsize):\n{s}"
    );
}

/// Every sandbox refusal names its reason (F2/F3): the run gesture
/// (Enter in the autofocused prompt) must never be silent, and an
/// unready pair must never fire a paid call.
#[test]
fn review_sandbox_refusals_name_reasons() {
    let mut h = harness();
    // Not connected: refuse with the connect teaching.
    h.goto_screen(6);
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("connect to the gateway first"),
        "disconnected run names its refusal: {notice}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. }))
            .is_none(),
        "no generation fires disconnected"
    );
    // Connected but nothing picked (providers still loading → no
    // fabricated selection to fall back on): refuse, name the gap.
    h.connect_as_admin();
    h.store.notice.set(None);
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("pick a provider first"),
        "unpicked run names the missing pick: {notice}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. }))
            .is_none(),
        "no generation fires without a pick"
    );
}

/// The redesign's shrink-pin matrix (findings 1020/1030 discipline):
/// the screen renders WHOLE at 110x34 and 80x24 in wizard mode (the
/// tightest chrome) across empty / completed / FAILED states — the
/// Failed error panel is the tallest pinned status (3 rows), so it is
/// the worst case the arithmetic must survive. No zero-collapse, no
/// fusion of content rows into block borders (the pre-fix defect
/// rendered the status line ON `╰…╯`, and a crushed journal scroll
/// bled rows over the Finish button).
#[test]
fn review_screen_renders_whole_at_both_sizes() {
    for (w, hgt) in [(110i32, 34i32), (80, 24)] {
        for state in ["empty", "ready", "failed"] {
            let mut h = harness_sized(Size::new(w, hgt));
            h.connect_as_admin();
            h.store.providers.set(Loadable::Ready(providers_fixture()));
            if state != "empty" {
                h.store.journal.update(|j| {
                    for i in 0..3 {
                        j.push(JournalEntry {
                            when: format!("10:0{i}:00Z"),
                            action: format!("PUT route output.route{i}"),
                            outcome: Ok("applied".into()),
                            verified: Some(Ok(format!("GET shows value{i}"))),
                        });
                    }
                });
            }
            match state {
                "ready" => h.store.sandbox.set(Loadable::Ready(SandboxOutcome {
                    ok: true,
                    error: None,
                    response: "short proof".into(),
                    routed_provider: Some("lmstudio".into()),
                    profile: None,
                    usage: None,
                    provider: "lmstudio".into(),
                    model: "test-model-a".into(),
                })),
                "failed" => {
                    h.store
                        .sandbox
                        .set(Loadable::Failed(abstractgateway_console::api::ApiError {
                            kind: abstractgateway_console::api::ApiErrorKind::Http(500),
                            message: "model exploded".into(),
                            body: None,
                            timed_out: false,
                        }))
                }
                _ => {}
            }
            // Wizard mode: the tightest chrome (goal row + Finish).
            h.ui.screen.set(6);
            let s = h.turns(3);
            let label = format!("{w}x{hgt} {state}");
            assert!(
                s.contains("run a REAL text generation"),
                "[{label}] teaching line:\n{s}"
            );
            assert!(
                s.contains("Generate (Enter)"),
                "[{label}] Generate button:\n{s}"
            );
            assert!(
                s.contains("Finish — switch to browse mode"),
                "[{label}] wizard finish reachable:\n{s}"
            );
            match state {
                "ready" => {
                    assert!(
                        s.contains("lmstudio / test-model-a"),
                        "[{label}] outcome names its pair:\n{s}"
                    );
                    assert!(
                        s.contains("PUT route output.route2"),
                        "[{label}] newest journal entry visible:\n{s}"
                    );
                }
                "failed" => {
                    assert!(
                        s.contains("HTTP 500") && s.contains("model exploded"),
                        "[{label}] failed outcome renders verbatim:\n{s}"
                    );
                    assert!(
                        s.contains("press g / Generate to retry"),
                        "[{label}] retry teaching survives:\n{s}"
                    );
                }
                _ => {
                    assert!(
                        s.contains("no test run yet"),
                        "[{label}] honest empty outcome:\n{s}"
                    );
                    assert!(
                        s.contains("no changes applied this session"),
                        "[{label}] honest empty journal:\n{s}"
                    );
                }
            }
            // Fusion guard: no content may land ON a border row (the
            // pre-fix defect rendered the status line on `╰…╯`).
            for line in s.lines() {
                let trimmed = line.trim_start();
                if trimmed.starts_with('╰') {
                    assert!(
                        !line.chars().any(|c| c.is_ascii_alphanumeric()),
                        "[{label}] content fused into a bottom border: {line:?}\n{s}"
                    );
                }
            }
        }
    }
}

/// The Providers-screen `t` contract after the redesign: it lands on
/// the Review screen with the provider pinned BY NAME (no modal).
#[test]
fn providers_t_jumps_to_inline_sandbox_prefilled() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(3);
    h.type_text("t");
    let s = h.turns(3);
    assert_eq!(
        h.ui.screen.get_untracked(),
        6,
        "t navigates to Review & Test:\n{s}"
    );
    let pinned = h.ui.sb_provider.get_untracked();
    assert!(
        pinned.starts_with("endpoint:"),
        "the selected profile's provider is pinned by name: {pinned:?}"
    );
    // The visible acknowledgment is the pinned pick itself: the picker
    // renders the provider name (notices are screen-scoped and would
    // be retired on arrival — the state change is the evidence).
    assert!(
        s.contains(&pinned),
        "the landing screen shows the pinned provider:\n{s}"
    );
}

#[test]
fn busy_strip_shows_elapsed_and_clears() {
    let mut h = harness();
    h.turn();
    h.store.begin_busy(991, "discovering models");
    let s = h.turns(2);
    assert!(s.contains("discovering models…"), "busy strip:\n{s}");
    h.store.end_busy(991);
    let s = h.turns(2);
    assert!(!s.contains("discovering models…"), "cleared:\n{s}");
}

#[test]
fn browse_mode_number_keys_jump_screens() {
    let mut h = harness();
    h.connect_as_admin();
    // Digits work from table screens (they die inside focused text
    // inputs by design — the connection screen needs Ctrl+N/P).
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("4");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 3, "4 → users screen");
    h.type_text("5");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 4, "5 → runtimes screen");
    // In wizard mode the number keys must NOT jump (gating).
    h.ui.wizard.set(true);
    h.ui.screen.set(2);
    h.turns(2);
    h.type_text("5");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 2, "wizard ignores digit jumps");
}

// =======================================================================
// Cycle-1 review hardening
// =======================================================================

/// P1 regression pin: a re-probe (new gateway / new token) must forget
/// every cached domain — stale tables from gateway A must never render
/// under gateway B's header.
#[test]
fn reprobe_resets_cached_domains() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.store.models.update(|m| {
        m.insert("lmstudio".into(), Loadable::Ready(vec!["m1".into()]));
    });
    // The F1 class: slots ADDED after the original fix must reset too —
    // a same-named entity on gateway B would otherwise wear gateway A's
    // substrate in the drawer.
    h.store.entity_detail.set(Loadable::Ready(
        abstractgateway_console::store::EntityDetail {
            name: "Testor".into(),
            substrate: Some(("old-gateway".into(), "old-model".into())),
            ..Default::default()
        },
    ));
    h.store.runs.set(Loadable::Ready(
        abstractgateway_console::store::RunsData::default(),
    ));
    let s = h.turns(2);
    assert!(s.contains("acme"), "cached data renders:\n{s}");

    // Back to connection, probe again (Enter submits the URL field —
    // Tab reaches it since the profiles table held focus after switch).
    h.goto_screen(0);
    h.ui.wizard.set(true);
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::Connect { .. })).is_some(),
        "probe sent"
    );
    assert!(
        matches!(h.store.profiles.get_untracked(), Loadable::NotAsked),
        "profiles forgotten on re-probe"
    );
    assert!(
        matches!(h.store.providers.get_untracked(), Loadable::NotAsked),
        "providers forgotten on re-probe"
    );
    assert!(
        h.store.models.with_untracked(|m| m.is_empty()),
        "model cache forgotten on re-probe"
    );
    assert!(
        matches!(h.store.entity_detail.get_untracked(), Loadable::NotAsked),
        "entity detail forgotten on re-probe (F1)"
    );
    assert!(
        matches!(h.store.runs.get_untracked(), Loadable::NotAsked),
        "runs forgotten on re-probe (F1)"
    );
}

/// F2 (proven stall): moving the entity selection while a detail load
/// is in flight must send a load for the NEW row once the stale result
/// lands — the drawer must never say "reading X…" with nothing loading.
#[test]
fn entity_detail_reloads_when_selection_moved_mid_flight() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    // Two entities so the selection can move.
    h.store.entities.set(Loadable::Ready(
        abstractgateway_console::store::entities_from_payload(&json!({
            "entities": [
                {"name": "Testor", "state": "asleep"},
                {"name": "Bestor", "state": "awake"},
            ]
        })),
    ));
    h.turns(2);
    // The selection effect asked for Testor; the slot is Loading.
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadEntityDetail { name } if name == "Testor"))
            .is_some(),
        "initial detail load for the selected row"
    );
    // Move the selection to Bestor while Testor's load is in flight…
    h.ui.entity_sel.set(1);
    h.turns(2);
    // …then Testor's result lands (stale for the current selection).
    h.store.entity_detail.set(Loadable::Ready(
        abstractgateway_console::store::EntityDetail {
            name: "Testor".into(),
            ..Default::default()
        },
    ));
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadEntityDetail { name } if name == "Bestor"))
            .is_some(),
        "stale landing re-fires the load for the CURRENT selection"
    );
}

/// The fabricated-selection law on a CONFIGURED row: the saved model is
/// preselected only under its own saved provider; switching provider
/// resets the pair to placeholders.
#[test]
fn route_editor_saved_pair_only_under_saved_provider() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    // Row 4 = output.voice (configured supertonic / supertonic-3;
    // supertonic is NOT a discoverable provider).
    for _ in 0..4 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("\r");
    let s = h.turns(2);
    assert!(s.contains("Route — Voice Output"), "editor open:\n{s}");
    assert!(
        s.contains("Applies now:") && s.contains("supertonic / supertonic-3"),
        "applies-now from server state:\n{s}"
    );
    // The models fetch for 'supertonic' fails (not a discovery provider)
    // — deliver the failure and expect the honest free-text lane with
    // the SAVED model prefilled, plus the discovery error verbatim.
    h.store.models.update(|m| {
        m.insert(
            "supertonic".into(),
            Loadable::Failed(abstractgateway_console::api::ApiError {
                kind: abstractgateway_console::api::ApiErrorKind::Http(404),
                message: "no models route for supertonic".into(),
                body: None,
                timed_out: false,
            }),
        );
    });
    let s = h.turns(2);
    assert!(s.contains("supertonic-3"), "saved model prefilled:\n{s}");
    assert!(
        s.contains("discovery failed") && s.contains("no models route for supertonic"),
        "discovery failure shown verbatim, not disguised as empty:\n{s}"
    );

    // Switch provider to lmstudio → the model resets to the placeholder
    // (never the old provider's model under the new provider).
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into(), "test-model-b".into()]),
        );
    });
    h.key(b"\t"); // mode radio (autofocused) → provider select
    h.turn();
    h.type_text("\r"); // open popup
    h.turns(2);
    // Popup opens highlighting the current value (supertonic, index 1);
    // one Down reaches lmstudio (whose models are injected Ready).
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r"); // commit lmstudio
    let s = h.turns(3);
    assert!(
        s.contains("choose a model…"),
        "model reset to placeholder after provider switch:\n{s}"
    );
    // The saved model may legitimately appear ONLY on the server-state
    // "Applies now" line — never in any picker/input row (a global
    // not-contains would be impossible; a disjunction with an
    // always-true arm pins nothing — cycle-2 finding).
    assert!(
        s.lines()
            .filter(|l| l.contains("supertonic-3"))
            .all(|l| l.contains("Applies now")),
        "the saved model never rides a different provider's pickers:\n{s}"
    );
}

/// Narrow terminals drop secondary columns — never the payload column.
#[test]
fn narrow_terminal_keeps_payload_columns() {
    let mut h = harness_sized(Size::new(80, 30));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    let s = h.turns(2);
    assert!(
        s.contains("supertonic-3"),
        "the model column keeps real width at 80 cols:\n{s}"
    );
    assert!(
        !s.lines().any(|l| l.contains("source")),
        "the source column drops at 80 cols instead of starving model:\n{s}"
    );
}

/// A second once-token waits for the first token modal to be dismissed —
/// an unread token is unrecoverable and must never be bulldozed.
#[test]
fn second_token_waits_for_first_modal_dismissal() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.ui.token_queue.update(|q| {
        q.push(("alice".into(), "agw_token_A".into()));
        q.push(("bob".into(), "agw_token_B".into()));
    });
    let s = h.turns(3);
    assert!(
        s.contains("Access token for 'alice'"),
        "first token modal:\n{s}"
    );
    assert!(
        !s.contains("agw_token_B"),
        "second token not shown yet:\n{s}"
    );
    // Dismiss the first (Esc) → the second opens via the epoch bump.
    h.press_escape();
    let s = h.turns(3);
    assert!(
        s.contains("Access token for 'bob'") && s.contains("agw_token_B"),
        "second token modal after dismissal:\n{s}"
    );
}

/// Cycle-2 P1 pin: a queued once-token must NOT open over a live
/// ChoicePrompt — same-z modal stacking gives the keyboard to the
/// OLDEST (invisible) layer, so arrows+Enter would drive the hidden
/// danger prompt while the token modal paints on top.
#[test]
fn token_waits_for_open_prompt() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.turns(2);
    // Open the rotate confirm (a ChoicePrompt, NOT in the modal slot).
    h.type_text("t");
    let s = h.turns(2);
    assert!(
        s.contains("Rotate the token for 'admin'"),
        "prompt open:\n{s}"
    );
    // A token arrives while the prompt is up.
    h.ui.token_queue
        .update(|q| q.push(("alice".into(), "agw_token_HELD".into())));
    let s = h.turns(3);
    assert!(
        !s.contains("agw_token_HELD"),
        "token modal must WAIT while a prompt owns the keys:\n{s}"
    );
    // Resolve the prompt (Enter commits the safe 'keep' default) — the
    // wrapped resolver decrements the count and the token opens.
    h.type_text("\r");
    let s = h.turns(3);
    assert!(
        s.contains("Access token for 'alice'") && s.contains("agw_token_HELD"),
        "token modal opens after the prompt resolves:\n{s}"
    );
}

/// Cycle-2 pin: Esc on a DIRTY form warns first (typed work must not
/// die on one keypress); the second Esc discards. Clean forms still
/// close on one Esc (pinned by escape_closes_form_modal).
#[test]
fn escape_on_dirty_form_warns_then_discards() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("a");
    let s = h.turns(2);
    assert!(s.contains("Add a provider connection"), "form open:\n{s}");
    // Type into the autofocused id field → dirty.
    h.type_text("acme3");
    h.turns(2);
    h.press_escape();
    let s = h.turns(2);
    assert!(
        s.contains("Add a provider connection"),
        "dirty form survives the first Esc:\n{s}"
    );
    assert!(
        s.contains("unsaved changes — press Esc again to discard"),
        "the warning names the second-Esc contract:\n{s}"
    );
    h.press_escape();
    let s = h.turns(2);
    assert!(
        !s.contains("Add a provider connection"),
        "second Esc discards:\n{s}"
    );
}

/// The dirty-guard's THIRD clause (round-2 P2-4): an edit after the
/// warning DISARMS it — the warning clears, and the next Esc must warn
/// anew instead of discarding (a warning shown minutes ago must never
/// make a later Esc silently destructive).
#[test]
fn dirty_guard_disarms_on_edit_after_warning() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("a");
    h.turns(2);
    h.type_text("acme3");
    h.turns(2);
    h.press_escape();
    let s = h.turns(2);
    assert!(
        s.contains("unsaved changes — press Esc again to discard"),
        "first Esc warns:\n{s}"
    );
    // Edit after the warning → disarm + clear the warning text.
    h.type_text("x");
    let s = h.turns(2);
    assert!(
        !s.contains("unsaved changes — press Esc again to discard"),
        "edit clears the warning:\n{s}"
    );
    // The next Esc warns AGAIN (does not discard).
    h.press_escape();
    let s = h.turns(2);
    assert!(
        s.contains("Add a provider connection"),
        "form survives the post-edit Esc:\n{s}"
    );
    assert!(
        s.contains("unsaved changes — press Esc again to discard"),
        "post-edit Esc re-warns:\n{s}"
    );
    h.press_escape();
    let s = h.turns(2);
    assert!(
        !s.contains("Add a provider connection"),
        "second Esc after re-arm discards:\n{s}"
    );
}

/// Web-console parity: the Runtimes screen renders the runtime-config
/// knob surface with per-knob PROVENANCE (value + which layer set it).
#[test]
fn runtime_knobs_render_with_provenance() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    // Knobs live behind a collapsed disclosure — the load fires on first
    // expand, never eagerly (2026-07-26 lazy law). Discoverability comes
    // from the folded header's wording and the `w` hint instead.
    h.turns(2);
    let _ = h.drain_cmds();
    h.ui.rt_knobs_folded.set(false);
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadRuntimeConfig))
            .is_some(),
        "expanding the knobs disclosure triggers the lazy load"
    );
    h.store.runtime_config.set(Loadable::Ready(
        abstractgateway_console::store::RuntimeConfigData::from_value(&json!({
            "writable": true,
            "workspace_root": {"value": "/srv/workspace", "source": "stored"},
            "workspace_allowed_paths": {"value": "/srv/archive\n/srv/notes", "source": "stored"},
            "workspace_blocked_paths": {"value": "/srv/secrets", "source": "stored"},
            "client_workspace_scope_overrides": {"value": true, "source": "stored"},
            "trust_client_launch_folder": {"value": true, "source": "stored"},
            "process_manager": {"value": false, "source": "default"},
            "executor": {"value": "codex", "source": "stored"},
            "operator_email": {"value": null, "source": "default"},
            "executors": [
                {"id": "codex", "display": "Codex CLI", "available": true, "default": true},
                {"id": "claude", "display": "Claude Code", "available": false, "default": false}
            ]
        })),
    ));
    let s = h.turns(2);
    assert!(s.contains("Runtime knobs"), "knob block:\n{s}");
    assert!(
        s.contains("default_workspace: /srv/workspace  (stored)"),
        "workspace root rendered:\n{s}"
    );
    assert!(
        s.contains("launch_folder_trust: true  (stored)"),
        "launch-folder trust knob rendered:\n{s}"
    );
    assert!(
        s.contains("scope_overrides: true  (stored)"),
        "advanced scope-overrides toggle rendered separately:\n{s}"
    );
    assert!(
        s.contains("Edit workspace access policy"),
        "admin editor entry point missing:\n{s}"
    );
    assert!(
        s.contains("executor: codex  (stored)"),
        "knob value + provenance:\n{s}"
    );
    assert!(
        s.contains("process_manager: false  (default)"),
        "bool knob rendered:\n{s}"
    );
    assert!(
        s.contains("operator_email: —  (default)"),
        "null renders as an honest dash:\n{s}"
    );
    assert!(
        s.contains("Codex CLI (default)") && s.contains("Claude Code — unavailable"),
        "executors availability-honest:\n{s}"
    );
}

/// The probe-acknowledgment law (operator incident 2026-07-23: pressing
/// Probe while already connected looked dead): every probe renders a
/// numbered, timestamped report line — same-outcome re-probes included.
#[test]
fn probe_report_line_renders_and_updates() {
    let mut h = harness();
    h.connect_as_admin();
    let s = h.turns(2);
    assert!(
        s.contains("no probe has run yet"),
        "honest empty state before any probe:\n{s}"
    );
    h.store
        .last_probe
        .set(Some(abstractgateway_console::store::ProbeReport {
            seq: 3,
            at: "21:14:02Z".into(),
            ok: true,
            outcome: "connected as admin@default (users), admin".into(),
            took_ms: 87,
        }));
    let s = h.turns(2);
    assert!(
        s.contains("last probe #3 at 21:14:02Z") && s.contains("✓ connected as admin@default"),
        "probe acknowledgment line:\n{s}"
    );
    // A later probe with the SAME outcome still visibly changes (#4, new time).
    h.store
        .last_probe
        .set(Some(abstractgateway_console::store::ProbeReport {
            seq: 4,
            at: "21:15:10Z".into(),
            ok: true,
            outcome: "connected as admin@default (users), admin".into(),
            took_ms: 91,
        }));
    let s = h.turns(2);
    assert!(
        s.contains("last probe #4 at 21:15:10Z"),
        "same-outcome re-probe still visibly acknowledged:\n{s}"
    );
}

/// The title bar is CHROME (operator screenshot 2026-07-24): a page
/// whose loaded content over-demands height used to flex-shrink the
/// fixed header row to ZERO — the tab bar painted at row 0 (engine
/// finding 0240's class at the root). Pin: header at row 0, the blank
/// separator under it, the tab bar at row 2 — on every screen, both
/// modes, WITH heavy fixtures, at a small terminal.
#[test]
fn title_bar_and_separator_survive_content_pressure() {
    let mut h = harness_sized(Size::new(100, 24));
    h.connect_as_admin();
    // Heavy fixtures everywhere the screenshots showed them.
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store.entities.set(Loadable::Ready(
        abstractgateway_console::store::entities_from_payload(&json!({
            "entities": [
                {"name": "Castor", "state": "asleep"},
                {"name": "Doorcheck", "state": "asleep"},
                {"name": "ephemeral", "state": "asleep"},
                {"name": "Hypnos", "state": "asleep"},
                {"name": "Mira", "state": "awake"},
                {"name": "Mnemosyne", "state": "asleep"},
                {"name": "Ossia", "state": "awake"},
            ]
        })),
    ));
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    for wizard in [true, false] {
        h.ui.wizard.set(wizard);
        for screen in 0..ui::SCREENS.len() {
            h.ui.screen.set(screen);
            let scr = h.turns(3);
            let lines: Vec<&str> = scr.lines().collect();
            assert!(
                lines[0].contains("AbstractGateway Console"),
                "title bar at row 0 (wizard={wizard} screen={screen}):\n{scr}"
            );
            assert!(
                lines[1].trim().is_empty(),
                "separator line under the title (wizard={wizard} screen={screen}):\n{scr}"
            );
            // With 8 tabs the bar OVERFLOWS at 110 cols and windows
            // (sticky around the active tab) — so the chrome check is
            // "the ACTIVE tab's title sits on row 2", which holds at
            // every screen; "1 Connection" is honestly behind ‹ when
            // the window has slid right.
            let want = format!("{} {}", ui::screen_key(screen), ui::SCREENS[screen]);
            assert!(
                lines[2].contains(&want),
                "tab bar at row 2 shows '{want}' (wizard={wizard} screen={screen}):\n{scr}"
            );
        }
    }
}

/// The centralized connection authority (round-4): a transport-class
/// domain failure while Connected flips the ONE conn signal to
/// Verifying and fires the probe exactly once.
#[test]
fn health_authority_verifies_then_retries_once() {
    let mut h = harness();
    let probes: Rc<RefCell<Vec<u64>>> = Rc::new(RefCell::new(Vec::new()));
    {
        let probes = probes.clone();
        *h.prober.borrow_mut() = Some(Box::new(move |_url, _token, gen| {
            probes.borrow_mut().push(gen);
        }));
    }
    h.connect_as_admin();
    h.goto_screen(1);
    h.turns(2);
    // Drain screen-entry loads so later asserts see only the retry.
    let _ = h.drain_cmds();

    // One domain fails at the transport layer.
    let net_err = || abstractgateway_console::api::ApiError {
        kind: abstractgateway_console::api::ApiErrorKind::Unreachable,
        message: "GET /x: Network Error: Connection reset by peer".into(),
        body: None,
        timed_out: false,
    };
    h.store.providers.set(Loadable::Failed(net_err()));
    let s = h.turns(3);
    assert!(
        matches!(h.store.conn.get_untracked(), ConnPhase::Verifying(_)),
        "transport failure flips conn to Verifying"
    );
    assert_eq!(probes.borrow().len(), 1, "prober fired exactly once");
    // The verifying story reaches the operator (header label + panel +
    // toast all carry it; the toast may overprint the header row in
    // this capture, so assert the story, not the pixel position).
    assert!(
        s.contains("verifying the gateway connection"),
        "the verifying story renders:\n{s}"
    );
    // The panel defers to the authority — no final-sounding error.
    assert!(
        s.contains("network hiccup"),
        "panel renders the verifying story:\n{s}"
    );

    // Probe settles OK → Connected again, the failed domain retried ONCE.
    let gen = h.store.probe_gen.get_untracked();
    abstractgateway_console::health::settle(h.store, &h.tx, gen, Ok(()));
    h.turns(2);
    assert!(
        h.store.conn.get_untracked().is_connected(),
        "settle(Ok) restores Connected"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadProviders)).is_some(),
        "the failed domain was retried"
    );
    // The SAME failure again: budget spent — no second probe.
    h.store.providers.set(Loadable::Failed(net_err()));
    h.turns(3);
    assert_eq!(
        probes.borrow().len(),
        1,
        "spent budget blocks a re-probe loop"
    );
    // User action re-arms the budget.
    h.store.conn_retry_spent.set(Vec::new());
    h.store.providers.set(Loadable::Failed(net_err()));
    h.turns(3);
    assert_eq!(probes.borrow().len(), 2, "user re-arm allows one more");
}

/// settle(Err) settles the ONE story every panel tells.
#[test]
fn health_authority_settles_down_with_one_story() {
    let mut h = harness();
    *h.prober.borrow_mut() = Some(Box::new(|_, _, _| {}));
    h.connect_as_admin();
    h.goto_screen(1);
    h.turns(2);
    h.store
        .providers
        .set(Loadable::Failed(abstractgateway_console::api::ApiError {
            kind: abstractgateway_console::api::ApiErrorKind::Unreachable,
            message: "boom".into(),
            body: None,
            timed_out: false,
        }));
    h.turns(3);
    let gen = h.store.probe_gen.get_untracked();
    abstractgateway_console::health::settle(
        h.store,
        &h.tx,
        gen,
        Err(abstractgateway_console::api::ApiError {
            kind: abstractgateway_console::api::ApiErrorKind::Unreachable,
            message: "connect refused".into(),
            body: None,
            timed_out: false,
        }),
    );
    let s = h.turns(2);
    assert!(
        matches!(h.store.conn.get_untracked(), ConnPhase::Unreachable(_)),
        "settle(Err) settles the connection phase"
    );
    assert!(
        s.contains("gateway connection lost"),
        "panels tell the ONE settled story:\n{s}"
    );
}

/// Engine 0.2.20 popup fix (finding 1050, operator screenshot): a
/// Select inside a centered Modal must open its popup ADJACENT to the
/// field, never displaced by the modal's origin to the top-left.
#[test]
fn select_popup_inside_modal_opens_adjacent_to_its_field() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    for _ in 0..6 {
        h.key(b"\x1b[B");
        h.turn();
    }
    h.type_text("\r");
    h.turns(2);
    h.key(b"\x1b[B");
    h.turns(2);
    h.key(b"\t");
    h.turn();
    h.type_text("\r");
    let s = h.turns(3);
    let rows: Vec<&str> = s.lines().collect();
    let field_row = rows
        .iter()
        .position(|l| l.contains("choose a provider…"))
        .expect("trigger renders");
    // The popup's first real option must sit within a few rows of the
    // trigger (below-preferred placement), and roughly at the same
    // column — the pre-fix displacement put it near the screen origin.
    let popup_row = rows
        .iter()
        .position(|l| l.contains("lmstudio"))
        .expect("popup option renders");
    let dist = popup_row.abs_diff(field_row);
    assert!(
        dist <= 6,
        "popup row {popup_row} adjacent to field row {field_row} (dist {dist}):\n{s}"
    );
    let field_col = rows[field_row].find("choose a provider…").unwrap();
    let popup_col = rows[popup_row].find("lmstudio").unwrap();
    assert!(
        field_col.abs_diff(popup_col) <= 8,
        "popup column {popup_col} aligned with field column {field_col}:\n{s}"
    );
}

/// Cycle-1 UX D1/D2: the FIRST-RUN screens must render whole at the
/// tight shipped size — a first-run gateway (0 entities) on Users and
/// a one-write journal on Review used to zero-collapse the entities
/// empty-state / the live-test teaching line (the inner-block shrink
/// class, siblings of findings 1020/1030).
#[test]
fn first_run_screens_survive_tight_height() {
    let mut h = harness_sized(Size::new(100, 24));
    h.connect_as_admin();
    // Users with ZERO entities (true first run) + a populated roster.
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store.entities.set(Loadable::Ready(Vec::new()));
    h.goto_screen(3);
    let s = h.turns(3);
    assert!(
        s.contains("no entities on this gateway"),
        "entities empty-state renders (not crushed):\n{s}"
    );

    // Review with one journal entry (the operator who did the wizard
    // right and saved one thing).
    h.store.journal.update(|j| {
        j.push(abstractgateway_console::store::JournalEntry {
            when: "12:00:00Z".into(),
            action: "PUT route output.text".into(),
            outcome: Ok("applied".into()),
            verified: Some(Ok("GET shows lmstudio".into())),
        })
    });
    h.goto_screen(6);
    let s = h.turns(3);
    assert!(
        s.contains("run a REAL text generation"),
        "live-test teaching line survives beside a journal entry:\n{s}"
    );
    assert!(
        s.contains("Generate (Enter)"),
        "the Generate button renders inside its block:\n{s}"
    );
}

/// D3: the engine's raw layout diagnostic is humanized in the footer
/// (it read as a crash log); the raw pointer stays behind the debug
/// flag. Non-layout notices pass through.
#[test]
fn engine_layout_notice_suppressed_in_non_debug() {
    use abstractgateway_console::ui::humanize_engine_notice;
    let raw = "layout: fixed-size child #0 LayoutId(Key { index: 22, generation: 6 })";
    let shown = humanize_engine_notice(raw);
    // Non-debug: layout diagnostics SUPPRESS (empty) — the engine
    // registry never clears, so a permanent operator banner is worse
    // than silence (REG-1). The raw pointer surfaces only with the flag.
    assert!(
        shown.is_empty(),
        "layout notice suppressed in non-debug: {shown:?}"
    );
    assert_eq!(
        humanize_engine_notice("caps: truecolor"),
        "caps: truecolor",
        "non-layout notices pass through"
    );
}

/// P1-B: wizard steps carry a goal line; browse mode shows none.
#[test]
fn wizard_steps_carry_a_goal_line() {
    let mut h = harness();
    h.connect_as_admin();
    h.ui.wizard.set(true);
    for (screen, needle) in [
        (1usize, "make one provider usable"),
        (2, "the engine picks models by default"),
        (3, "mint a token"),
        (4, "storage inventory"),
        (5, "the registered workflows"),
        (6, "run one real test"),
        (7, "live models"),
    ] {
        h.ui.screen.set(screen);
        let s = h.turns(2);
        assert!(
            s.contains("Step goal:") && s.contains(needle),
            "wizard screen {screen} goal line:\n{s}"
        );
    }
    // Browse mode: no goal line.
    h.ui.wizard.set(false);
    h.ui.screen.set(1);
    let s = h.turns(2);
    assert!(
        !s.contains("Step goal:"),
        "browse mode has no goal line:\n{s}"
    );
}

/// P1-C: the create profile form shows the 4-field happy path with
/// advanced fields folded behind "More options"; edit mode opens them
/// (the operator is deliberately changing an existing profile).
#[test]
fn profile_form_folds_advanced_fields_on_create() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.turns(2);
    h.type_text("a");
    let s = h.turns(2);
    // Happy path visible.
    assert!(s.contains("family"), "family in happy path:\n{s}");
    assert!(s.contains("API key"), "API key in happy path:\n{s}");
    // The disclosure header is present…
    assert!(s.contains("More options"), "disclosure header:\n{s}");
    // …and the advanced fields are folded away (scope radio hidden).
    assert!(
        !s.contains("just this login"),
        "scope folded on create:\n{s}"
    );
    h.press_escape();
    h.turns(2);

    // Edit mode opens the disclosure: scope is visible immediately.
    h.type_text("e");
    let s = h.turns(2);
    assert!(
        s.contains("just this login"),
        "edit mode opens the advanced fields:\n{s}"
    );
}

/// REG-1 (cycle-3): the wizard goal row must be TRULY zero-height in
/// browse mode and absent on the connection screen — an empty pinned
/// row pushed the macOS-default 80x24 connection screen over 24 rows,
/// crushing the connected-badges and pinning a permanent "display
/// degraded" footer (engine notices never clear). The header/separator/
/// tab-bar must still occupy rows 0-2 with the connected badge whole.
#[test]
fn connection_screen_fits_at_macos_default_80x24() {
    let mut h = harness_sized(Size::new(80, 24));
    h.connect_as_admin();
    // Wizard mode on the connection screen: the tightest first frame.
    h.ui.wizard.set(true);
    h.ui.screen.set(0);
    let s = h.turns(3);
    let lines: Vec<&str> = s.lines().collect();
    assert!(
        lines[0].contains("AbstractGateway Console"),
        "title survives:\n{s}"
    );
    // The connected identity badge (admin@default) must render — it was
    // the row crushed at 80x24 before the fix.
    assert!(
        s.contains("admin@default"),
        "connected identity badge renders at 80x24:\n{s}"
    );
    // No connection-screen goal row (it is self-teaching).
    assert!(
        !s.contains("Step goal:"),
        "connection screen has no goal row:\n{s}"
    );
    // No permanent degradation banner in the footer.
    assert!(
        !s.contains("display degraded"),
        "no display-degraded banner at the default size:\n{s}"
    );
}

/// REG-2 (cycle-3): the entity-partition line is grammatical at n>=2.
#[test]
fn entity_partition_line_plural_grammar() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&json!({
            "users": [
                {"user_id": "a", "roles": ["entity"], "principal_kind": "entity"},
                {"user_id": "b", "roles": ["entity"], "principal_kind": "entity"},
            ]
        }))));
    let s = h.turns(2);
    assert!(
        s.contains("2 entities also hold their own access tokens"),
        "plural grammar:\n{s}"
    );
    assert!(
        !s.contains("hold its own"),
        "no singular-possessive at n>=2:\n{s}"
    );
}

// =======================================================================
// Double-click-to-edit (COMPLAINT A) + runtime→runs follow (COMPLAINT B)
// =======================================================================

use abstractgateway_console::store::{RunScope, RunsData};

/// SGR mouse press+release at 1-based cell (x, y).
fn click_at(h: &mut Harness, x: usize, y: usize) {
    h.key(format!("\x1b[<0;{x};{y}M").as_bytes());
    h.key(format!("\x1b[<0;{x};{y}m").as_bytes());
}

/// Both presses of a double-click in one input batch: the engine's
/// click chain counts presses within the time window + cell tolerance
/// (the Driver publishes event time each turn, so two presses fed
/// together land well inside the window).
fn double_click_at(h: &mut Harness, x: usize, y: usize) {
    click_at(h, x, y);
    click_at(h, x, y);
}

/// 1-based row of the first rendered line containing `needle`.
fn find_row(screen: &str, needle: &str) -> usize {
    screen
        .lines()
        .position(|l| l.contains(needle))
        .unwrap_or_else(|| panic!("'{needle}' not on screen:\n{screen}"))
        + 1
}

fn runs_fixture_rows(n: usize, status: &str) -> Vec<abstractgateway_console::store::RunRow> {
    (0..n)
        .map(|i| abstractgateway_console::store::RunRow {
            run_id: format!("run-{i:04}-aaaaaaaa"),
            workflow_id: format!("wf-{i}@0.0.1"),
            status: status.to_string(),
            updated_at: "2026-07-25T10:00:00Z".into(),
            paused: false,
            parent_run_id: None,
        })
        .collect()
}

/// The 2026-07-26 directive, pinned: entering the Runtimes screen
/// loads ONLY the inventory — no runs, no data homes, no runtime
/// config. The detail region teaches instead of loading.
#[test]
fn runtimes_screen_loads_nothing_eagerly() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    let s = h.turns(3);
    let sent = h.drain_cmds();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::LoadRuntimes)),
        "the inventory itself loads: {sent:?}"
    );
    assert!(
        sent.iter().all(|c| !matches!(
            c,
            Cmd::LoadRuns { .. } | Cmd::LoadDataHomes { .. } | Cmd::LoadRuntimeConfig
        )),
        "NOTHING below the inventory loads before a choice: {sent:?}"
    );
    assert!(
        s.contains("select a runtime above"),
        "the detail region teaches the gesture:\n{s}"
    );
    assert!(
        !s.contains(" Runs ") || !s.contains(" Cache "),
        "no tabs before a choice:\n{s}"
    );
}

/// The choose gesture: clicking a runtime row (or Enter on the
/// highlighted one) opens the tabbed inspector and loads THAT plane's
/// sessions (a scoped LoadRuns — runs live in per-plane stores, so
/// following the choice means switching endpoints, not filtering
/// rows). An empty entity plane explains why empty is normal.
#[test]
fn runs_panel_follows_runtime_selection() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    let _ = h.drain_cmds();

    // Choose the entity plane row with the keyboard (arrow moves the
    // selection = the same on_select a mouse click fires).
    h.key(b"\x1b[B");
    h.turns(2);
    let sent = h.find_cmd(|c| matches!(c, Cmd::LoadRuns { .. }));
    match sent {
        Some(Cmd::LoadRuns {
            scope:
                RunScope::Plane {
                    kind,
                    tenant_id,
                    runtime_id,
                    label,
                },
            ..
        }) => {
            assert_eq!(kind, "entity");
            assert_eq!(tenant_id, "default");
            assert_eq!(runtime_id, "runtime_testor");
            assert_eq!(label, "Testor");
        }
        other => panic!("expected a plane-scoped LoadRuns, got {other:?}"),
    }
    assert!(
        matches!(h.store.runs.get_untracked(), Loadable::Loading),
        "runs slot set Loading synchronously with the send"
    );
    let s = h.turns(1);
    assert!(
        s.contains("Runs") && s.contains("Cache"),
        "the inspector tabs render once a runtime is chosen:\n{s}"
    );
    assert!(
        s.contains("loading this runtime's runs…"),
        "the load names itself while in flight:\n{s}"
    );

    // The plane answers empty — the empty state names WHERE and WHY
    // (operator 2026-07-26: chats/life days don't create runtime runs).
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Plane {
            kind: "entity".into(),
            tenant_id: "default".into(),
            runtime_id: "runtime_testor".into(),
            label: "Testor".into(),
        },
        rows: vec![],
    }));
    let s = h.turns(2);
    assert!(
        s.contains("no durable runs in Testor's plane"),
        "empty state names the plane AND the why:\n{s}"
    );
    assert!(
        s.contains("entity plane: Testor"),
        "scope line names the plane:\n{s}"
    );
}

/// Choose discipline: arrowing across runtime rows while a plane load
/// is in flight sends NOTHING (Loading holds); when the stale result
/// lands, ONE corrective load targets the row the operator is actually
/// on (the users-screen F2 warm-keeper shape).
#[test]
fn runs_selection_no_send_storm_and_corrects() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    let _ = h.drain_cmds();

    // Choose the default row (Enter on the highlighted row 0) — the
    // Own-lane load goes out and stays in flight.
    h.type_text("\r");
    h.turns(2);
    assert!(matches!(h.store.runs.get_untracked(), Loadable::Loading));
    let _ = h.drain_cmds();

    // Wiggle: down to the entity row, back up, down again — every move
    // re-chooses, but Loading holds (no send storm).
    h.key(b"\x1b[B");
    h.turns(2);
    h.key(b"\x1b[A");
    h.turns(2);
    h.key(b"\x1b[B");
    h.turns(2);
    assert!(
        h.drain_cmds()
            .iter()
            .all(|c| !matches!(c, Cmd::LoadRuns { .. })),
        "Loading holds — no send storm while arrowing"
    );

    // The stale Own result lands while the entity row is chosen →
    // exactly one corrective plane load.
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: vec![],
    }));
    h.turns(2);
    let loads: Vec<Cmd> = h
        .drain_cmds()
        .into_iter()
        .filter(|c| matches!(c, Cmd::LoadRuns { .. }))
        .collect();
    assert_eq!(loads.len(), 1, "exactly one corrective load: {loads:?}");
    assert!(
        matches!(
            &loads[0],
            Cmd::LoadRuns {
                scope: RunScope::Plane { runtime_id, .. },
                ..
            } if runtime_id == "runtime_testor"
        ),
        "the correction targets the CURRENT row: {loads:?}"
    );
}

/// The Data & cache tab: loads the registry lazily on first look and
/// attributes stores to the chosen plane by longest data_dir prefix —
/// the default plane additionally lists shared (outside-every-plane)
/// caches; purge refuses protected stores.
#[test]
fn data_tab_lazy_load_and_attribution() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    // Choose the ENTITY plane, Sessions tab active: no data-homes load.
    h.key(b"\x1b[B");
    h.turns(2);
    let _ = h.drain_cmds();
    assert!(
        matches!(h.store.data_homes.get_untracked(), Loadable::NotAsked),
        "data homes untouched while Sessions is the active tab"
    );

    // Switch to the Cache tab (index 2; Artifacts sits at 1) → ONE lazy load.
    h.ui.rt_tab.set(2);
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadDataHomes { .. }))
            .is_some(),
        "first look at the Data tab loads the registry"
    );
    h.store.data_homes.set(Loadable::Ready(
        abstractgateway_console::store::data_homes_from_payload(&json!({
            "homes": [
                {"name": "testor-artifacts", "path": "/tmp/runtime/entities/testor/artifacts",
                 "kind": "artifacts", "owner": "gateway", "safe_to_purge": true,
                 "description": "entity artifact store"},
                {"name": "gateway-root-store", "path": "/tmp/runtime/artifacts",
                 "kind": "artifacts", "owner": "gateway", "safe_to_purge": false,
                 "description": "root artifact store"},
                {"name": "abstractcore-blocs", "path": "/tmp/dot-abstractcore/blocs",
                 "kind": "prompt-cache", "owner": "abstractcore", "safe_to_purge": true,
                 "description": "shared cache outside every plane"}
            ]
        })),
    ));
    let s = h.turns(2);
    // Entity plane: ONLY its own store (nesting under the default root
    // must not leak the root store in, nor the shared cache).
    assert!(
        s.contains("testor-artifacts"),
        "the entity plane's store renders:\n{s}"
    );
    assert!(
        !s.contains("gateway-root-store") && !s.contains("abstractcore-blocs"),
        "root + shared stores stay off a nested plane's tab:\n{s}"
    );
    assert!(
        s.contains("data dir: /tmp/runtime/entities/testor"),
        "the plane's dir fact renders:\n{s}"
    );

    // The default plane sees its own PURGEABLE stores AND the shared
    // caches; durable stores (safe_to_purge=false) are NOT caches and
    // never render on the Cache tab (operator 2026-08-19).
    h.key(b"\x1b[A");
    let s = h.turns(3);
    assert!(
        s.contains("abstractcore-blocs"),
        "default plane lists the shared caches:\n{s}"
    );
    assert!(
        !s.contains("gateway-root-store"),
        "durable stores are not caches — kept off the Cache tab:\n{s}"
    );
    assert!(
        s.contains("shared (outside planes)"),
        "shared rows carry the label:\n{s}"
    );
    assert!(
        !s.contains("testor-artifacts"),
        "the entity's store belongs to the entity plane, not default:\n{s}"
    );
}

/// COMPLAINT B honesty: cancel/steer on a foreign plane's run refuse
/// with the mechanism named — the durable command would land in this
/// console's own inbox and sit unconsumed forever (live-verified:
/// POST /commands appends to the caller's world, no cross-plane
/// routing).
#[test]
fn foreign_plane_cancel_and_steer_refused() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.key(b"\x1b[B"); // choose the entity plane
    h.turns(2);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Plane {
            kind: "entity".into(),
            tenant_id: "default".into(),
            runtime_id: "runtime_testor".into(),
            label: "Testor".into(),
        },
        rows: runs_fixture_rows(1, "running"),
    }));
    h.turns(2);
    let _ = h.drain_cmds();

    h.type_text("c");
    h.turns(2);
    assert!(
        h.drain_cmds()
            .iter()
            .all(|c| !matches!(c, Cmd::CancelRun { .. })),
        "no cancel command for a foreign plane"
    );
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("ticked by that plane's own runtime"),
        "refusal names the mechanism: {notice}"
    );

    h.type_text("s");
    let s = h.turns(2);
    assert!(
        !s.contains("Steer run"),
        "no steer form for a foreign plane:\n{s}"
    );
}

/// COMPLAINT A on the runs table: double-click (and Enter) = the row's
/// non-destructive modal, steer — through the same guard as the `s`
/// key (terminal runs refuse with the reason; cancel stays a
/// deliberate keypress + confirm, never a casual double-click).
#[test]
fn run_double_click_opens_steer_form() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    // Choose the default plane (Enter on the highlighted row 0).
    h.type_text("\r");
    h.turns(2);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: runs_fixture_rows(1, "running"),
    }));
    let s = h.turns(3);
    let y = find_row(&s, "run-0000");
    double_click_at(&mut h, 6, y);
    let s = h.turns(3);
    assert!(
        s.contains("Steer run"),
        "double-click on a running run opens steer:\n{s}"
    );
}

/// COMPLAINT A, users table, the full unselected-row gesture: click 1
/// selects, click 2 activates → the edit modal (same body as `e`).
#[test]
fn double_click_opens_user_editor() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities_fixture())));
    let s = h.turns(2);
    // alice is row 1 — NOT the selected row (selection starts at 0).
    let y = find_row(&s, "alice");
    double_click_at(&mut h, 4, y);
    let s = h.turns(3);
    assert!(
        s.contains("Edit user 'alice'"),
        "double-click selects then opens the editor:\n{s}"
    );
    assert_eq!(
        h.ui.user_sel.get_untracked(),
        1,
        "first press moved the selection to the clicked row"
    );
}

/// COMPLAINT A, entities table: double-click on the (already selected)
/// entity row opens the manage menu — the `m` action.
#[test]
fn double_click_opens_entity_manage_menu() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(3);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_fixture())));
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities_fixture())));
    let s = h.turns(2);
    let y = find_row(&s, "Testor");
    double_click_at(&mut h, 4, y);
    let s = h.turns(3);
    assert!(
        s.contains("Manage entity 'Testor'"),
        "double-click opens the manage menu:\n{s}"
    );
}

/// COMPLAINT A, providers unified table: Enter on the focused table
/// activates the selected row into the edit form (the table consumes
/// Enter now that an activation is bound — same body as `e`).
#[test]
fn enter_activates_profile_editor() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    h.type_text("\r");
    let s = h.turns(3);
    assert!(
        s.contains("Edit profile 'acme'"),
        "Enter activates the selected profile into its editor:\n{s}"
    );
}

/// COMPLAINT A, routes table: double-click edits the selected route —
/// the same per-row editability guards as Enter/e (read-only rows
/// refuse with the reason).
#[test]
fn double_click_opens_route_editor() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    let s = h.turns(2);
    let y = find_row(&s, "output.voice");
    double_click_at(&mut h, 4, y);
    let s = h.turns(3);
    assert!(
        s.contains("Route — Voice Output"),
        "double-click opens the route editor:\n{s}"
    );
}

// =======================================================================
// 2026-07-26 redesign adversarial probes (fable5 review)
// =======================================================================

/// P1 pin: the steer modal must survive a runs-region re-render. The
/// runs region is a dyn that rebuilds whenever `store.runs` changes
/// (post-cancel refresh_runs, a stale-load correction) — a modal whose
/// scope hangs off that GENERATION dies with it: the tree unmounts but
/// the modal LAYER keeps owning every key (a locked app wearing stale
/// pixels). Anchor: the page scope, like every other screen's modals.
#[test]
fn steer_modal_survives_runs_region_rerender() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r"); // choose the default plane
    h.turns(2);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: runs_fixture_rows(2, "running"),
    }));
    let s = h.turns(3);
    let y = find_row(&s, "run-0000");

    // Baseline: the drive itself works — open, type, Tab to Send, Enter.
    double_click_at(&mut h, 6, y);
    let s = h.turns(3);
    assert!(s.contains("Steer run"), "steer modal open:\n{s}");
    h.type_text("baseline");
    h.turns(2);
    h.key(b"\t");
    h.turns(1);
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SteerRun { .. })) {
        Some(Cmd::SteerRun { guidance, .. }) => assert_eq!(guidance, "baseline"),
        other => panic!("baseline steer drive broken: {other:?}"),
    }
    h.turns(2);

    // Round two: a background op lands while the modal is open (the
    // exact shape of refresh_runs after a cancel): the runs region
    // regenerates under the open modal.
    double_click_at(&mut h, 6, y);
    let s = h.turns(3);
    assert!(s.contains("Steer run"), "steer modal open again:\n{s}");
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: runs_fixture_rows(2, "running"),
    }));
    h.turns(2);

    // The modal must still be FULLY ALIVE: the guidance input accepts
    // text and Send delivers it (signals created on the modal's scope
    // die if the scope hung off the regenerated runs region).
    h.type_text("wrap it up");
    h.turns(2);
    h.key(b"\t"); // focus: guidance -> Send button
    h.turns(1);
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SteerRun { .. })) {
        Some(Cmd::SteerRun { guidance, .. }) => assert_eq!(
            guidance, "wrap it up",
            "typed guidance survives a runs-region re-render"
        ),
        other => panic!("steer modal dead after runs-region re-render: {other:?}"),
    }
    let s = h.turns(2);
    assert!(
        !s.contains("Steer run"),
        "send closed the steer modal:\n{s}"
    );
}

/// P1 pin: a re-probe (possibly a DIFFERENT gateway/principal) must
/// forget the chosen runtime. `ui.rt_detail` is a cloned RuntimeRow —
/// remote data — and surviving the reset makes the sessions effect
/// load the OLD plane against the NEW gateway with zero user choice
/// on it (the F1 stale-domain class in UI clothing), rendering the
/// old row's facts under the new header until the self-heal lands.
#[test]
fn gateway_reset_forgets_the_chosen_runtime() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.key(b"\x1b[B"); // choose the entity plane
    h.turns(2);
    assert!(h.ui.rt_detail.get_untracked().is_some(), "plane chosen");
    let _ = h.drain_cmds();

    // Re-probe through the REAL UI path (Enter in the URL field →
    // connect_now → reset_domains), same drive as
    // reprobe_resets_cached_domains.
    h.goto_screen(0);
    h.ui.wizard.set(true);
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::Connect { .. })).is_some(),
        "probe sent"
    );
    assert!(
        h.ui.rt_detail.get_untracked().is_none(),
        "the chosen runtime is REMOTE data — it must not survive a gateway reset"
    );

    // Back on the runtimes screen: ONLY the inventory loads; no
    // stale-choice LoadRuns fires against the new gateway.
    h.goto_screen(4);
    h.turns(3);
    let sent = h.drain_cmds();
    assert!(
        sent.iter().all(|c| !matches!(c, Cmd::LoadRuns { .. })),
        "no runs load for a stale pre-reset choice: {sent:?}"
    );
    let s = h.turns(1);
    assert!(
        s.contains("select a runtime above"),
        "teaching line back after the reset:\n{s}"
    );
}

/// P1/P2 pin: the busy heartbeat (store.tick, bumped every 500ms while
/// any op is in flight) must not steal focus. The inventory dyn reads
/// the tick for its loading spinner; every regeneration used to
/// re-mount the table's `.autofocus()` and yank focus off the tabs
/// bar — turning the next arrow key into a plane switch (which LOADS).
#[test]
fn busy_tick_does_not_steal_focus_from_the_tabs_bar() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.key(b"\x1b[B"); // choose the entity plane (focus on the inventory)
    h.turns(2);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Plane {
            kind: "entity".into(),
            tenant_id: "default".into(),
            runtime_id: "runtime_testor".into(),
            label: "Testor".into(),
        },
        rows: vec![],
    }));
    h.turns(2);

    // Reach the tabs bar with the keyboard and prove it owns ←/→.
    h.key(b"\t");
    h.turns(1);
    h.key(b"\x1b[C"); // Right → Data & cache
    h.turns(2);
    assert_eq!(
        h.ui.rt_tab.get_untracked(),
        1,
        "tabs bar reachable via Tab; Right switches"
    );
    h.key(b"\x1b[D"); // Left → Sessions
    h.turns(2);
    assert_eq!(h.ui.rt_tab.get_untracked(), 0);
    let _ = h.drain_cmds();

    // The busy heartbeat ticks while some op runs elsewhere.
    h.store.tick.update(|t| *t += 1);
    h.turns(2);

    // Focus must still be on the tabs bar: Right switches tabs…
    h.key(b"\x1b[C");
    h.turns(2);
    assert_eq!(
        h.ui.rt_tab.get_untracked(),
        1,
        "focus stays on the tabs bar across busy ticks"
    );
    // …and an arrow key must NOT move the inventory selection (a moved
    // selection is a CHOOSE — it loads another plane nobody picked).
    let chosen_before = h.ui.rt_detail.get_untracked();
    h.key(b"\x1b[B");
    h.turns(2);
    assert_eq!(
        h.ui.rt_detail.get_untracked(),
        chosen_before,
        "arrows after a busy tick must not re-choose a plane"
    );
}

/// Layout pin: the redesigned screen must FIT at the macOS default
/// 80x24 — inventory header, inspector tabs, and the footer hints all
/// visible with a runtime chosen (the 0240 shrink class).
#[test]
fn runtimes_inspector_fits_at_80x24() {
    let mut h = harness_sized(Size::new(80, 24));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    let s = h.turns(2);
    assert!(
        s.contains("select a runtime above"),
        "teaching line visible at 80x24:\n{s}"
    );
    h.type_text("\r"); // choose the default plane
    h.turns(2);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: runs_fixture_rows(3, "running"),
    }));
    let s = h.turns(2);
    assert!(
        s.contains("Runs") && s.contains("Cache"),
        "inspector tabs visible at 80x24:\n{s}"
    );
    assert!(
        s.contains("run-0000"),
        "at least one run row visible at 80x24:\n{s}"
    );
    assert!(
        s.contains("kind") && s.contains("runtime"),
        "inventory table header survives at 80x24:\n{s}"
    );
    assert!(
        s.contains("Runtime knobs"),
        "knobs disclosure header survives at 80x24:\n{s}"
    );
    assert!(s.contains("focus"), "footer hints survive at 80x24:\n{s}");
}

/// Honesty pin: a user plane the gateway lists with `data_dir: null`
/// (a binding that never materialized) must render an honest dash on
/// the Data tab, never a blank after "data dir:".
#[test]
fn unmaterialized_plane_data_dir_renders_a_dash() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&json!({
            "runtimes": [
                {"kind": "default", "tenant_id": "default", "runtime_id": "default",
                 "label": "Gateway default runtime", "data_dir": "/tmp/runtime"},
                {"kind": "user", "tenant_id": "default", "runtime_id": "ghost",
                 "label": "default/ghost", "data_dir": null, "materialized": false}
            ]
        }))));
    h.turns(2);
    h.key(b"\x1b[B"); // choose the unmaterialized user plane
    h.turns(2);
    h.ui.rt_tab.set(2); // Cache (Artifacts sits at 1)
    h.store.data_homes.set(Loadable::Ready(vec![]));
    let s = h.turns(3);
    assert!(
        s.contains("data dir: —"),
        "an unmaterialized plane's dir renders an honest dash:\n{s}"
    );
}

/// Attribution tie documentation: if two planes ever carry the SAME
/// data_dir (the gateway serves per-user dirs today, so this is a
/// payload-shape guard, not a live repro), longest-prefix cannot
/// break the tie — the FIRST plane in payload order wins and the
/// second plane's Data tab shows nothing. Pinned so a payload change
/// upstream turns this from documented arbitrariness into a visible
/// test conversation.
#[test]
fn home_plane_index_duplicate_dirs_first_wins() {
    use abstractgateway_console::store::{home_plane_index, RuntimeRow};
    let plane = |kind: &str, rid: &str, dir: &str| RuntimeRow {
        kind: kind.into(),
        tenant_id: "default".into(),
        runtime_id: rid.into(),
        label: rid.into(),
        owners: vec![],
        data_dir: dir.into(),
        size_bytes: None,
        size_note: None,
        state: None,
        liveness: None,
        note: None,
    };
    let planes = vec![
        plane("default", "default", "/tmp/runtime"),
        plane("user", "alice", "/tmp/runtime/users/default"),
        plane("user", "bob", "/tmp/runtime/users/default"),
    ];
    // Tie between alice and bob: strict-greater keeps the FIRST.
    assert_eq!(
        home_plane_index(&planes, "/tmp/runtime/users/default/store"),
        Some(1),
        "duplicate data_dirs: first plane in payload order wins (documented)"
    );
    // Empty data_dir never owns anything.
    let planes2 = vec![
        plane("user", "ghost", ""),
        plane("default", "default", "/tmp/runtime"),
    ];
    assert_eq!(home_plane_index(&planes2, "/tmp/runtime/x"), Some(1));
}

/// Gesture-gap probe (engine Table policy): a single click on the
/// ALREADY-selected row fires neither on_select (no change) nor
/// on_activate (click_count 1) — so the very first click most
/// operators make (row 0 is pre-highlighted) cannot be the ONLY
/// taught gesture. The teaching line must name the working gestures.
#[test]
fn teaching_line_names_working_gestures_for_the_highlighted_row() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    let s = h.turns(2);
    // The dead gesture, demonstrated: one click on the pre-selected row
    // (the label column ellipsizes, so match the un-truncated head).
    let y = find_row(&s, "Gateway default");
    click_at(&mut h, 6, y);
    h.turns(3);
    assert!(
        h.ui.rt_detail.get_untracked().is_none(),
        "engine fact this test documents: a single click on the selected row is DEAD"
    );
    // Therefore the teaching must offer Enter/double-click explicitly.
    let s = h.turns(1);
    assert!(
        s.contains("select a runtime above") && s.contains("double-click the highlighted"),
        "teaching line names a gesture that works on the highlighted row:\n{s}"
    );
    // And the taught gesture must actually work: double-click chooses.
    double_click_at(&mut h, 6, y);
    h.turns(3);
    assert!(
        h.ui.rt_detail.get_untracked().is_some(),
        "double-click on the highlighted row chooses it"
    );
}

/// Buttons must actually light under the pointer.
///
/// `run_cli` arms `RunConfig::hover_ink` (mode 1003, motion with no button
/// held) — without it the engine's Button hover visuals exist but never
/// receive an event, which is how this console shipped through 0.2.23. The
/// harness feeds mouse bytes straight to the Driver, so what this pins is
/// the half that can regress silently in OUR code: that the Buttons we
/// build are hover-reactive and restore cleanly on leave. That the real
/// binary still ENABLES 1003 is the pty smoke's job.
///
/// Table has no hover state in the engine — the inventory tables staying
/// inert is expected, not a bug.
#[test]
fn buttons_take_hover_ink_and_release_it() {
    let mut h = harness();
    h.connect_as_admin();
    let s = h.turns(2);
    let y = find_row(&s, "Re-probe");
    let cx = s.lines().nth(y - 1).unwrap().find("Re-probe").unwrap() + 3;

    let paint_at = |h: &mut Harness, y0: i32| -> Vec<String> {
        let scr = h.term.screen();
        (0..150)
            .map(|x| match scr.cell(x, y0) {
                Some(c) => format!("{:?}", c.paint),
                None => String::new(),
            })
            .collect()
    };
    // SGR 35 = motion, no button held — exactly what mode 1003 delivers.
    let park = b"\x1b[<35;1;34M";

    h.key(park);
    h.turns(2);
    let away = paint_at(&mut h, y as i32 - 1);

    h.key(format!("\x1b[<35;{};{}M", cx + 1, y).as_bytes());
    h.turns(2);
    let over = paint_at(&mut h, y as i32 - 1);

    h.key(park);
    h.turns(2);
    let back = paint_at(&mut h, y as i32 - 1);

    let lit = away.iter().zip(over.iter()).filter(|(a, b)| a != b).count();
    assert!(
        lit >= 8,
        "pointing at the Re-probe Button must re-ink its label \
         (only {lit} cells changed paint)"
    );
    assert_eq!(
        away, back,
        "leaving the Button must restore its base paint exactly"
    );
}

/// A sandbox response pane that SHRINKS must still show all of itself.
///
/// abstracttui 0.2.x skipped `Scroll`'s offset repair when content solved
/// to exactly the viewport height, so a pane scrolled down and then
/// refilled with something shorter could keep a stale offset and paint
/// blank (the engine's 0281 field bug, fixed in 0.3.0). This console's
/// reply pane turned out never to show it — the reply is typeset as
/// markdown, so it reflows to far fewer rows than it has lines and the
/// exact-height condition is not reachable from a plain reply. Pinned
/// anyway: this is the one pane here whose content genuinely shrinks,
/// and the guard costs nothing.
#[test]
fn sandbox_pane_survives_a_response_that_shrinks() {
    let mut h = harness();
    h.connect_as_admin();
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-b".into());
    h.goto_screen(6);
    h.turns(2);

    let outcome = |body: &str| SandboxOutcome {
        ok: true,
        error: None,
        response: body.to_string(),
        routed_provider: Some("lmstudio".into()),
        profile: None,
        usage: None,
        provider: "lmstudio".into(),
        model: "test-model-b".into(),
    };

    // A reply far taller than the pane, so there is room to scroll.
    let long: String = (0..80)
        .map(|i| format!("line {i:02} of the long reply\n"))
        .collect();
    h.store.sandbox.set(Loadable::Ready(outcome(&long)));
    let s = h.turns(2);
    let y = find_row(&s, "of the long reply");
    for _ in 0..6 {
        h.key(format!("\x1b[<65;20;{y}M").as_bytes());
        h.turns(1);
    }

    // Now a reply that fits: every item must be on screen, wherever the
    // markdown reflow puts it.
    const N: usize = 14;
    let short: String = (0..N).map(|i| format!("exact {i:02} row\n")).collect();
    h.store.sandbox.set(Loadable::Ready(outcome(&short)));
    let s = h.turns(3);
    let missing: Vec<String> = (0..N)
        .map(|i| format!("exact {i:02}"))
        .filter(|tok| !s.contains(tok.as_str()))
        .collect();
    assert!(
        missing.is_empty(),
        "a shorter reply must repair any stale offset and paint in full \
         (missing {missing:?}):\n{s}"
    );
}

// =======================================================================
// WEIGHTS: the `d` verb (model downloads on the execution host)
// =======================================================================

/// The weights column, in the SAME four words the AbstractCore console
/// and the web console print — and NO banner, because every route on
/// this host is answered.
///
/// The absent artifact here is the recommended text build on a host that
/// routes text at its own model. The column still reports it honestly
/// (the route's own weights are genuinely not downloaded); what must not
/// happen is the screen presenting it as a host-level shortfall the
/// operator is expected to fix.
#[test]
fn routes_screen_shows_weight_availability_and_no_banner_when_every_route_is_answered() {
    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .availability
        .set(Loadable::Ready(availability_fixture()));
    let s = h.turns(2);
    assert!(
        !s.contains("no model yet") && !s.contains("recommended:"),
        "a fully routed host is never told it is short of a model:\n{s}"
    );
    assert!(
        s.contains("not downloaded"),
        "absent weights read plainly:\n{s}"
    );
    assert!(
        s.contains("installed"),
        "present weights read plainly:\n{s}"
    );
    assert!(s.contains("weights"), "the column is labelled:\n{s}");
    assert!(
        s.contains("download weights"),
        "the hint row offers the verb on THIS screen:\n{s}"
    );
}

/// ...and the host the banner DOES exist for: a route with nothing
/// routed to it, whose recommended model is not on disk. It names the
/// route, names the ARTIFACT (`@4bit`, not the served id a route would
/// store), and keeps the actionable verb.
#[test]
fn routes_screen_banners_only_the_routes_with_no_model_at_all() {
    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .availability
        .set(Loadable::Ready(availability_fixture_fresh_install()));
    let s = h.turns(2);
    assert!(
        s.contains("1 route with no model yet"),
        "the banner counts ROUTES that need one, not catalog entries:\n{s}"
    );
    assert!(
        s.contains("input.text"),
        "it names the route the operator has to answer:\n{s}"
    );
    assert!(
        s.contains("qwen/qwen3.5-9b@4bit"),
        "the banner names the ARTIFACT, not the served id:\n{s}"
    );
    assert!(
        s.contains("w downloads"),
        "the actionable verb survives the elastic list:\n{s}"
    );
}

/// `a` applies the framework recommendation to the execution host —
/// the same key, prompt and vocabulary as the AbstractCore console-TUI
/// and the gateway console's "Apply recommended" button. Safe by
/// default; replacing the operator's own routes is a separate answer.
#[test]
fn a_applies_the_recommended_routes() {
    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    let s = h.turns(2);
    assert!(
        s.contains("a applies the recommended routes"),
        "the banner names the verb:\n{s}"
    );
    h.drain_cmds();

    h.key(b"a");
    let s = h.turns(2);
    assert!(
        s.contains("Apply the framework's recommended routes"),
        "the prompt asks first:\n{s}"
    );
    assert!(h.drain_cmds().is_empty(), "nothing before the answer");
    // The DEFAULT answer keeps the operator's routes.
    h.key(b"\r");
    h.turns(2);
    let dbg = format!("{:?}", h.drain_cmds());
    assert!(
        dbg.contains("ApplyRecommendedRoutes") && dbg.contains("force: false"),
        "the default answer never overrules the operator: {dbg}"
    );

    // The second answer is the explicit overrule.
    h.key(b"a");
    h.turns(2);
    h.key(b"\x1b[B");
    h.turn();
    h.key(b"\r");
    h.turns(2);
    let dbg = format!("{:?}", h.drain_cmds());
    assert!(
        dbg.contains("ApplyRecommendedRoutes") && dbg.contains("force: true"),
        "the danger answer forces: {dbg}"
    );
}

/// The journal line says BOTH halves: what changed and what was KEPT.
/// A summary that only listed the writes would make "nothing happened
/// to my text route" look like a silent failure instead of the safety
/// rule it is.
#[test]
fn applied_recommended_summary_names_changes_and_what_was_kept() {
    use abstractgateway_console::worker::applied_recommended_summary;
    let payload = serde_json::json!({
        "applied_recommended": {"routes": [
            {"key": "output.image", "action": "apply", "changed": true,
             "before": {}, "after": {"provider": "mlx-gen", "model": "flux.2-klein"}},
            {"key": "input.text", "action": "kept", "changed": false,
             "before": {"provider": "lmstudio", "model": "qwen3-0.6b"},
             "after": {"provider": "lmstudio", "model": "qwen3-0.6b"}},
            {"key": "output.voice", "action": "already", "changed": false,
             "before": {"provider": "supertonic", "model": "supertonic-3"},
             "after": {"provider": "supertonic", "model": "supertonic-3"}}
        ]}
    });
    let summary = applied_recommended_summary(&payload);
    assert!(summary.contains("output.image"), "{summary}");
    assert!(summary.contains("mlx-gen/flux.2-klein"), "{summary}");
    assert!(
        summary.contains("kept yours on input.text (lmstudio/qwen3-0.6b)"),
        "the kept route is named with its value: {summary}"
    );
    // A no-op run says so rather than printing an empty line.
    let none = serde_json::json!({"applied_recommended": {"routes": [
        {"key": "input.text", "action": "already", "changed": false,
         "before": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"},
         "after": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}}
    ]}});
    assert_eq!(
        applied_recommended_summary(&none),
        "every recommended route already matched"
    );
}

/// `w` on an ABSENT row confirms first, names the artifact and the
/// provider, and only then sends the download — the one command in this
/// console that spends gigabytes never fires on a single keystroke.
#[test]
fn w_confirms_then_downloads_the_recommended_artifact() {
    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .availability
        .set(Loadable::Ready(availability_fixture()));
    h.turns(2);
    h.drain_cmds();

    h.ui.route_sel.set(0); // input.text — weights absent
    h.key(b"w");
    let s = h.turns(2);
    assert!(
        s.contains("Download qwen/qwen3.5-9b@4bit with lmstudio"),
        "the confirm names artifact and provider:\n{s}"
    );
    assert!(
        h.drain_cmds().is_empty(),
        "nothing downloads before the operator confirms"
    );
    // Danger confirms default to the SAFE option; move to Download.
    h.key(b"\x1b[A");
    h.turn();
    h.key(b"\r");
    h.turns(2);
    let dbg = format!("{:?}", h.drain_cmds());
    assert!(
        dbg.contains("DownloadModel") && dbg.contains("qwen/qwen3.5-9b@4bit"),
        "the download names the artifact: {dbg}"
    );
    assert!(
        dbg.contains("LoadAvailability"),
        "the weights are re-probed right after: {dbg}"
    );
}

/// The three refusals, each with its reason. `unknown` is the important
/// one: guessing there spends the execution host's disk.
///
/// The verb is `w` (weights), not `d`: `d` DELETES on the Providers and
/// Users screens, and a safe, frequent action must not share a key with a
/// destructive one two screens over.
#[test]
fn w_refuses_with_a_reason_instead_of_guessing() {
    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .availability
        .set(Loadable::Ready(availability_fixture()));
    h.turns(2);
    h.drain_cmds();

    h.ui.route_sel.set(4); // output.voice — installed
    h.key(b"w");
    let s = h.turns(2);
    assert!(s.contains("already installed"), "installed refusal:\n{s}");
    assert!(
        h.drain_cmds().is_empty(),
        "no download for an installed model"
    );

    h.ui.route_sel.set(5); // output.image.text_to_image — unknown
    h.key(b"w");
    let s = h.turns(2);
    assert!(
        s.contains("availability is unknown"),
        "unknown is never treated as absent:\n{s}"
    );
    assert!(
        h.drain_cmds().is_empty(),
        "no download on an unknown answer"
    );

    h.ui.route_sel.set(6); // input.sound — unconfigured, no weights row
    h.key(b"w");
    let s = h.turns(2);
    assert!(
        s.contains("no weight information yet"),
        "an unprobed route says so:\n{s}"
    );
    assert!(h.drain_cmds().is_empty(), "no download without evidence");
}

/// A live download outranks the summary line: the banner shows the
/// gateway's OWN job status while one is running.
#[test]
fn a_running_download_takes_over_the_banner() {
    use abstractgateway_console::store::DownloadStatus;

    let mut h = harness_sized(Size::new(150, 44));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(routes_fixture()));
    h.store
        .availability
        .set(Loadable::Ready(availability_fixture()));
    h.store
        .download
        .set(Some(DownloadStatus::from_value_for_test(
            "job1",
            "lmstudio",
            "qwen/qwen3.5-9b@4bit",
            "running",
            "pulling layer",
            Some(42.0),
            12.0,
        )));
    let s = h.turns(2);
    assert!(
        s.contains("download:"),
        "the live line takes the banner:\n{s}"
    );
    assert!(s.contains("42%"), "progress is shown:\n{s}");
    assert!(s.contains("pulling layer"), "the gateway's own words:\n{s}");
}

/// A routes payload carrying the artifacts from the operator's own
/// screenshot: two `wan2.2` builds that share a 24-character prefix and
/// differ only near the end, plus a long source module. Kept local to
/// the width tests so the shared fixture stays about row SEMANTICS.
fn wide_routes_fixture() -> RoutesData {
    RoutesData::from_value(&json!({
        "ok": true, "writable": true,
        "authority": "abstractcore.gateway_runtime",
        "source": "abstractcore.gateway_runtime",
        "errors": [],
        "routes": [
            {"key": "output.video.text_to_video", "kind": "output", "modality": "video",
             "label": "Video Generation", "task": "text_to_video",
             "provider": "mlx-gen",
             "model": "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "output.video.image_to_video", "kind": "output", "modality": "video",
             "label": "Video From Image", "task": "image_to_video",
             "provider": "mlx-gen",
             "model": "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.text", "kind": "input", "modality": "text",
             "label": "Text Input", "provider": "endpoint:airelay", "model": "gpt-5.4",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.image", "kind": "input", "modality": "image",
             "label": "Image Input", "provider": "endpoint:airelay", "model": "gpt-5.4",
             "source": "abstractcore.gateway_runtime", "configured": true,
             "covered_by": "input.text", "read_only": true}
        ]
    }))
}

/// The routes grid as rendered: every data row under the header, up to
/// the first blank one, with the screen block's borders stripped. Row
/// LABELS belong to the row model; this helper only cares which cells
/// the width policy drew.
fn grid_rows(screen: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut inside = false;
    for l in screen.lines() {
        let body = l.trim_matches(|c| c == '│' || c == ' ');
        if body.starts_with("route ") {
            inside = true;
            continue;
        }
        if inside {
            if body.is_empty() {
                break;
            }
            out.push(body.to_string());
        }
    }
    out
}

/// THE OPERATOR'S BUG REPORT, rendered: "don't truncate the text in the
/// column when not necessary".
///
/// At 200 cells this grid has room for every artifact, provider id and
/// source module it carries, so it must print them WHOLE. The old policy
/// spent constants (`Cells(28)`, `Cells(20)`, `ellipsize(model, 40)`) and
/// gave the slack to a Flex model column, which is how
/// `AbstractFramework/wan2.2-t2v-a14b-diffu…` came to sit beside seventy
/// blank cells.
#[test]
fn routes_grid_prints_whole_names_when_the_terminal_has_room() {
    let mut h = harness_sized(Size::new(200, 34));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(wide_routes_fixture()));
    let s = h.turns(2);
    for whole in [
        "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
        "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit",
        "abstractcore.gateway_runtime",
        "endpoint:airelay",
        "covered by input.text",
    ] {
        assert!(s.contains(whole), "{whole:?} must print whole at 200:\n{s}");
    }
    let grid = grid_rows(&s);
    assert!(grid.len() >= 4, "the grid rendered:\n{s}");
    assert!(
        !grid.iter().any(|l| l.contains('\u{2026}')),
        "no ellipsis belongs in a 200-cell grid:\n{}",
        grid.join("\n")
    );
}

/// And when the terminal is genuinely too narrow, the cut keeps the end
/// that tells rows apart. `…-t2v-a14b-diffusers-8bit` and
/// `…-i2v-a14b-diffusers-8bit` are one head-first cut away from being
/// the same string on screen — which is the failure the middle ellipsis
/// exists to prevent.
#[test]
fn narrow_routes_grid_keeps_the_discriminating_tail() {
    let mut h = harness_sized(Size::new(110, 34));
    h.connect_as_admin();
    h.goto_screen(2);
    h.store.routes.set(Loadable::Ready(wide_routes_fixture()));
    let s = h.turns(2);
    let grid = grid_rows(&s);
    let cell = |tag: &str| -> String {
        grid.iter()
            .find(|l| l.contains(tag))
            .unwrap_or_else(|| panic!("no {tag} row:\n{s}"))
            .clone()
    };
    let t2v = cell("t2v");
    let i2v = cell("i2v");
    assert!(
        t2v.contains('\u{2026}'),
        "110 cells really is too narrow for these artifacts: {t2v:?}"
    );
    assert!(
        t2v.contains("t2v-a14b-diffusers-8bit") && i2v.contains("i2v-a14b-diffusers-8bit"),
        "the discriminating tail survives the cut: {t2v:?} / {i2v:?}"
    );
    assert!(
        !t2v.contains("AbstractFramework/wan2.2-t2v"),
        "the cell really was cut (otherwise this test proves nothing): {t2v:?}"
    );
    // The closed vocabulary keeps its whole word at every width.
    assert!(
        s.contains("covered by input.text"),
        "the state vocabulary is not squeezed into nonsense:\n{s}"
    );
}

/// WEB-CONSOLE PARITY (operator 2026-08-19: "exactly the same design and
/// parity with the console-tui for runtime"). The four tabs each carry a
/// toolbar line: the active filter, the query, the page position, and the
/// gestures that change them — folded into the panel's existing status
/// line because 80x24 owns no spare rows.
#[test]
fn runtime_tabs_show_toolbar_state_and_gestures() {
    use abstractgateway_console::store::{ArtifactRow, ArtifactsData, LogFileRow};

    // Wide harness: this pin tests CONTENT (state + taught gestures), not
    // how the 108-column footer elides — that is its own layout pin.
    let mut h = harness_sized(Size::new(170, 30));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r"); // choose the default plane
    h.turns(2);

    // --- Runs tab: status + query + page + keys on one line ---
    h.ui.rt_runs_status.set("running".into());
    h.ui.rt_runs_query.set("coder".into());
    h.ui.rt_runs_offset.set(100);
    h.store.runs.set(Loadable::Ready(RunsData {
        status: "running".into(),
        query: "coder".into(),
        root_only: true,
        offset: 100,
        has_more: true,
        scope: RunScope::Own,
        rows: runs_fixture_rows(3, "running"),
    }));
    let s = h.turns(2);
    assert!(
        s.contains("status=running"),
        "status filter on the line:\n{s}"
    );
    assert!(s.contains("q=\"coder\""), "query on the line:\n{s}");
    assert!(s.contains("101–103"), "page position on the line:\n{s}");
    // Gestures are taught once, in the footer hint bar (repeating them on
    // every panel line truncated the state at 108 columns).
    assert!(
        s.contains("f filter") && s.contains("search") && s.contains("n/p"),
        "the footer teaches the toolbar gestures:\n{s}"
    );

    // --- Artifacts tab: same shape, with the real total ---
    h.ui.rt_tab.set(1);
    h.ui.rt_art_modality.set("image".into());
    h.store.artifacts.set(Loadable::Ready(ArtifactsData {
        rows: vec![ArtifactRow {
            name: "hero.png".into(),
            kind: "image".into(),
            size_bytes: Some(2400),
            run_id: "44cd3017".into(),
            created_at: "2026-08-19T02:30:41".into(),
            artifact_id: "a-1".into(),
            content_type: "image/png".into(),
            content_path: "/tmp/store/ab/cd.bin".into(),
            workflow_id: "image-gen@1.2".into(),
            session_id: String::new(),
        }],
        total: 79147,
        has_more: true,
        offset: 0,
        modality: "image".into(),
        query: String::new(),
    }));
    let s = h.turns(2);
    assert!(s.contains("hero.png"), "artifact row renders:\n{s}");
    assert!(s.contains("type=image"), "type filter named:\n{s}");
    assert!(
        s.contains("of 79147"),
        "the real total, never a 'newest N' cap:\n{s}"
    );

    // --- Logs tab: FILES (not homes), with the filter line ---
    h.ui.rt_tab.set(3);
    h.store.logs.set(Loadable::Ready(vec![LogFileRow {
        name: "abstractgateway.log".into(),
        home: "gateway-logs-54566bd9".into(),
        size_bytes: Some(5_700_000),
        modified_at: "2026-08-19T07:12:44".into(),
    }]));
    let s = h.turns(2);
    assert!(
        s.contains("abstractgateway.log") && s.contains("Enter tail"),
        "log FILES list with the tail gesture:\n{s}"
    );
}

/// A cache is a cache, in BOTH consoles: the Cache tab lists disposable
/// stores plus stale registrations (the hygiene surface, with Forget) —
/// never durable stores, never logs.
#[test]
fn cache_tab_lists_caches_and_stale_rows_only() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    h.ui.rt_tab.set(2);
    h.store.data_homes.set(Loadable::Ready(
        abstractgateway_console::store::data_homes_from_payload(&json!({
            "homes": [
                {"name": "abstractcore-blocs", "path": "/tmp/dot/blocs", "kind": "prompt-cache",
                 "owner": "abstractcore", "safe_to_purge": true, "description": "KV cache",
                 "exists": true, "size_bytes": 168},
                {"name": "gateway-logs-x", "path": "/tmp/runtime/logs", "kind": "logs",
                 "owner": "gateway", "safe_to_purge": true, "description": "logs", "exists": true},
                {"name": "gateway-artifacts-x", "path": "/tmp/runtime/artifacts", "kind": "artifacts",
                 "owner": "gateway", "safe_to_purge": false, "description": "durable", "exists": true},
                {"name": "gateway-artifacts-dead", "path": "/tmp/gone/artifacts", "kind": "artifacts",
                 "owner": "gateway", "safe_to_purge": true, "description": "stale", "exists": false}
            ]
        })),
    ));
    let s = h.turns(3);
    assert!(s.contains("abstractcore-blocs"), "caches list:\n{s}");
    assert!(
        !s.contains("gateway-logs-x"),
        "logs are their own tab, never caches:\n{s}"
    );
    assert!(
        !s.contains("gateway-artifacts-x"),
        "durable stores are not caches:\n{s}"
    );
    assert!(
        s.contains("gateway-artifacts-dead") && s.contains("stale row"),
        "stale registrations surface HERE with an honest marker:\n{s}"
    );
}

/// OPERATOR BUGS (2026-08-19): (a) every list tab capped at the THIRD row
/// because Artifacts/Logs borrowed `home_sel`, whose clamp is sized by the
/// CACHE row count; (b) the filter dropdown and search box were invisible
/// (gesture-only). Both pinned here.
#[test]
fn list_tabs_have_own_selection_and_a_visible_toolbar() {
    use abstractgateway_console::store::{ArtifactRow, ArtifactsData, LogFileRow};

    let mut h = harness_sized(Size::new(150, 34));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r"); // choose the default plane
    h.turns(2);

    // THREE caches (the operator's real shape) — the ceiling that leaked.
    h.store.data_homes.set(Loadable::Ready(
        abstractgateway_console::store::data_homes_from_payload(&json!({
            "homes": [
                {"name": "c1", "path": "/tmp/dot/a", "kind": "prompt-cache", "owner": "core",
                 "safe_to_purge": true, "description": "", "exists": true, "size_bytes": 1},
                {"name": "c2", "path": "/tmp/dot/b", "kind": "artifacts", "owner": "core",
                 "safe_to_purge": true, "description": "", "exists": true, "size_bytes": 2},
                {"name": "c3", "path": "/tmp/dot/c", "kind": "model-cache", "owner": "core",
                 "safe_to_purge": true, "description": "", "exists": true, "size_bytes": 3}
            ]
        })),
    ));
    // SIX log files — selection must reach the sixth, not stop at the third.
    let files: Vec<LogFileRow> = (0..6)
        .map(|i| LogFileRow {
            name: format!("log-{i}.log"),
            home: "gateway-logs-x".into(),
            size_bytes: Some(100),
            modified_at: format!("2026-08-19T07:0{i}:00"),
        })
        .collect();
    h.store.logs.set(Loadable::Ready(files));
    h.ui.rt_tab.set(3);
    let s = h.turns(3);
    assert!(
        s.contains("all log homes"),
        "the filter DROPDOWN is visible:\n{s}"
    );
    assert!(
        s.contains("search log files"),
        "the SEARCH box is visible:\n{s}"
    );

    // Reach past the old ceiling. The CLAMP is what capped the operator at
    // line 3 (three caches -> max index 2 -> the third row), so the pin
    // drives the selection directly and asserts nothing pulls it back.
    h.ui.rt_logs_sel.set(5);
    h.turns(3);
    assert_eq!(
        h.ui.rt_logs_sel.get_untracked(),
        5,
        "logs selection is its OWN signal — the cache row count must not cap it"
    );
    assert_eq!(
        h.ui.home_sel.get_untracked(),
        0,
        "the cache tab's selection is untouched by walking the logs list"
    );

    // Artifacts: same, with more rows than there are caches.
    let rows: Vec<ArtifactRow> = (0..5)
        .map(|i| ArtifactRow {
            name: format!("a-{i}.png"),
            kind: "image".into(),
            size_bytes: Some(10),
            run_id: "r1".into(),
            created_at: "2026-08-19T02:30:41".into(),
            artifact_id: format!("id-{i}"),
            content_type: "image/png".into(),
            content_path: format!("/tmp/store/id-{i}.png"),
            workflow_id: "wf".into(),
            session_id: String::new(),
        })
        .collect();
    h.store.artifacts.set(Loadable::Ready(ArtifactsData {
        rows,
        total: 5,
        has_more: false,
        offset: 0,
        modality: String::new(),
        query: String::new(),
    }));
    h.ui.rt_tab.set(1);
    let s = h.turns(3);
    assert!(
        s.contains("all types"),
        "artifacts filter dropdown visible:\n{s}"
    );
    assert!(
        s.contains("search artifacts"),
        "artifacts search box visible:\n{s}"
    );
    h.ui.rt_art_sel.set(4);
    h.turns(3);
    assert_eq!(
        h.ui.rt_art_sel.get_untracked(),
        4,
        "artifacts selection reaches the fifth row (cache count must not cap it)"
    );
    // And the clamp still WORKS on its own tab: past the end snaps back.
    h.ui.rt_art_sel.set(99);
    h.turns(3);
    assert_eq!(
        h.ui.rt_art_sel.get_untracked(),
        4,
        "the artifacts clamp is sized by the artifacts rows"
    );
}

/// IMAGES PREVIEW IN THE TERMINAL (operator 2026-08-19: "you should be
/// able to preview images thanks to abstracttui"). The engine decodes
/// PNG/JPEG and draws bitmaps as cell mosaics, so an image artifact
/// renders here instead of being refused with a "open it in the web
/// console" note.
#[test]
fn image_artifact_previews_as_a_bitmap() {
    use abstractgateway_console::store::{ArtifactRow, ArtifactsData};
    use abstracttui::prelude::{Bitmap, Rgba};
    use std::sync::Arc;

    let mut h = harness_sized(Size::new(150, 34));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    h.ui.rt_tab.set(1);
    h.store.artifacts.set(Loadable::Ready(ArtifactsData {
        rows: vec![ArtifactRow {
            name: "shot.png".into(),
            kind: "image".into(),
            size_bytes: Some(1_200_000),
            run_id: "r1".into(),
            created_at: "2026-08-02T03:35:42".into(),
            artifact_id: "art-1".into(),
            content_type: "image/png".into(),
            content_path: "/tmp/store/ab/cd.bin".into(),
            workflow_id: String::new(),
            session_id: "sess-1".into(),
        }],
        total: 1,
        has_more: false,
        offset: 0,
        modality: String::new(),
        query: String::new(),
    }));
    h.turns(2);

    // Open the detail: the image lane must ASK for bytes, not refuse.
    h.ui.rt_art_sel.set(0);
    h.type_text("o"); // `o` opens the highlighted row from anywhere
    let s = h.turns(3);
    assert!(
        !s.contains("cannot render in a terminal"),
        "images are no longer refused:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadArtifactImage { .. }))
            .is_some(),
        "the modal requests the image bytes for decoding"
    );

    // The decoded bitmap lands: the mosaic paints (no CodeView text).
    let mut bmp = Bitmap::new(32, 16, Rgba::TRANSPARENT);
    for y in 0..16 {
        for x in 0..32 {
            bmp.set(x, y, Rgba::new(220, 40, 90, 255));
        }
    }
    h.store.artifact_image.set(Some(Arc::new(bmp)));
    let s = h.turns(3);
    assert!(
        s.contains("shot.png") && s.contains("image/png"),
        "the header keeps naming the artifact:\n{s}"
    );
    assert!(
        !s.contains("decoding image…"),
        "the placeholder gives way to the picture:\n{s}"
    );
}

/// LONG TEXT SCROLLS (operator 2026-08-19: "how come i can't scroll
/// through the log file content"). `CodeView` windows its draw but takes
/// the offset from the app — nothing drove it, so every log and JSON
/// preview sat frozen on line 1.
#[test]
fn log_tail_scrolls_through_its_content() {
    let mut h = harness_sized(Size::new(150, 34));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    h.ui.rt_tab.set(3);
    h.store.logs.set(Loadable::Ready(vec![
        abstractgateway_console::store::LogFileRow {
            name: "big.log".into(),
            home: "gateway-logs-x".into(),
            size_bytes: Some(4096),
            modified_at: "2026-08-19T07:12:44".into(),
        },
    ]));
    h.turns(2);
    h.ui.rt_logs_sel.set(0);
    h.type_text("o"); // open the tail
    h.turns(2);

    // 60 numbered lines land: the top of the file shows, the tail does not.
    let body: String = (1..=60).map(|i| format!("line-{i:03}\n")).collect();
    h.store.log_text.set(Some(body));
    let s = h.turns(3);
    assert!(s.contains("line-001"), "the first line renders:\n{s}");
    assert!(
        !s.contains("line-055"),
        "a deep line is off-screen at first:\n{s}"
    );
    assert!(s.contains("line 1/60"), "the position is stated:\n{s}");

    // End jumps to the bottom — the content MOVES.
    h.key(b"\x1b[F"); // End
    let s = h.turns(3);
    assert!(
        s.contains("line-060") || s.contains("line 60/60"),
        "End reaches the end of the file:\n{s}"
    );

    // Home comes back.
    h.key(b"\x1b[H");
    let s = h.turns(3);
    assert!(s.contains("line 1/60"), "Home returns to the top:\n{s}");

    // THE MOUSE WHEEL SCROLLS TOO (operator 2026-08-19). SGR wheel-down
    // over the viewer: `CSI < 65 ; col ; row M`, three lines per notch.
    h.key(b"\x1b[<65;60;12M");
    let s = h.turns(3);
    assert!(
        s.contains("line 4/60"),
        "one wheel notch scrolls three lines:\n{s}"
    );
    h.key(b"\x1b[<65;60;12M");
    h.key(b"\x1b[<65;60;12M");
    let s = h.turns(3);
    assert!(s.contains("line 10/60"), "the wheel keeps scrolling:\n{s}");
    // And back up.
    h.key(b"\x1b[<64;60;12M");
    let s = h.turns(3);
    assert!(s.contains("line 7/60"), "wheel-up scrolls back:\n{s}");
}

/// Operator 2026-08-20: `*.jpg` in a runtime tab's search box found
/// nothing — every filter was a plain substring, so the `*` was compared
/// literally. Driven here through the REAL Cache tab, which filters in
/// this process (Runs and Artifacts filter server-side).
#[test]
fn a_filetype_glob_filters_the_cache_tab() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.key(b"\x1b[B"); // choose a plane — the Inspect pane is empty until then
    h.turns(2);
    h.ui.rt_tab.set(2);
    h.turns(2);
    h.store.data_homes.set(Loadable::Ready(
        abstractgateway_console::store::data_homes_from_payload(&json!({
            "homes": [
                {"name": "shots", "path": "/tmp/runtime/entities/testor/artifacts/shots.jpg",
                 "kind": "artifacts", "owner": "gateway", "safe_to_purge": true,
                 "description": "image outputs"},
                {"name": "blocs", "path": "/tmp/runtime/entities/testor/cache/blocs.db",
                 "kind": "prompt-cache", "owner": "abstractcore", "safe_to_purge": true,
                 "description": "prompt cache"}
            ]
        })),
    ));
    let all = h.turns(2);
    assert!(
        all.contains("shots") && all.contains("blocs"),
        "both rows:\n{all}"
    );

    // The reported defect: a filetype pattern must SELECT, not empty the
    // table. `*` crosses `/`, so it reaches into the stored path.
    h.ui.rt_cache_query.set("*.jpg".into());
    let jpg = h.turns(2);
    assert!(
        jpg.contains("shots"),
        "the .jpg row survives `*.jpg`:\n{jpg}"
    );
    assert!(
        !jpg.contains("blocs"),
        "the .db row is filtered out:\n{jpg}"
    );

    // The substring half is untouched — this is what shipped before.
    h.ui.rt_cache_query.set("blocs".into());
    let sub = h.turns(2);
    assert!(
        sub.contains("blocs") && !sub.contains("shots"),
        "substring:\n{sub}"
    );

    // A glob that matches nothing empties the table rather than matching
    // everything (the failure mode a broken anchor would produce).
    h.ui.rt_cache_query.set("*.png".into());
    let none = h.turns(2);
    assert!(
        !none.contains("shots") && !none.contains("blocs"),
        "no rows:\n{none}"
    );
}

/// The search modal states the rule ONCE, above the field — the glob half
/// is undiscoverable otherwise, and the box is where a user meets it.
#[test]
fn the_search_modal_teaches_the_glob_half() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.key(b"\x1b[B"); // choose a plane — `/` refuses without one
    h.turns(2);
    h.ui.rt_tab.set(1);
    h.turns(2);
    // `/` opens the active tab's search box.
    h.key(b"/");
    let s = h.turns(2);
    assert!(s.contains("Search artifacts"), "the modal opened:\n{s}");
    assert!(
        s.contains("*.jpg"),
        "the modal names the glob half by example:\n{s}"
    );
    assert!(
        s.contains("substring"),
        "and names the half a plain query still gets:\n{s}"
    );
}

/// Operator 2026-08-20: opening a .jpeg preview flashed another
/// artifact's message before the image appeared.
///
/// The worker is ONE serial lane and `artifact_text` / `artifact_image`
/// are GLOBAL slots. Open two image artifacts in a row and the first
/// one's result arrives AFTER the second modal is on screen, painting
/// under the wrong header until the right result lands. Clearing the
/// slots at open — which the code already did — cannot fix that: the
/// stale result had not been produced yet. `preview_target` is stamped
/// at open and the worker drops anything else.
#[test]
fn a_stale_preview_result_never_paints_under_the_next_artifact() {
    use abstractgateway_console::store::{artifact_preview_key, ArtifactRow, ArtifactsData};

    let art = |id: &str, name: &str| ArtifactRow {
        name: name.into(),
        kind: "image".into(),
        size_bytes: Some(231_500),
        run_id: "run-1".into(),
        created_at: "2026-06-13T09:57:24".into(),
        artifact_id: id.into(),
        content_type: "image/jpeg".into(),
        content_path: "/tmp/store/ab/cd.bin".into(),
        workflow_id: String::new(),
        session_id: String::new(),
    };

    let mut h = harness_sized(Size::new(170, 30));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r"); // choose the default plane
    h.turns(2);
    h.ui.rt_tab.set(1);
    // The panel re-requests unless the fixture matches the toolbar's
    // (modality, query, offset) — otherwise it paints "loading artifacts…".
    h.ui.rt_art_modality.set("image".into());
    h.store.artifacts.set(Loadable::Ready(ArtifactsData {
        rows: vec![art("a-slow", "slow.jpeg"), art("a-wanted", "lpa.jpeg")],
        total: 2,
        has_more: false,
        offset: 0,
        modality: "image".into(),
        query: String::new(),
    }));
    h.turns(2);

    // Open the FIRST artifact, then the second before the first answers.
    // `o` opens the active tab's highlighted row from anywhere (Enter
    // needs the table to hold focus, which it does not here).
    h.type_text("o");
    h.turns(2);
    assert!(
        h.store
            .preview_wanted(&artifact_preview_key("run-1", "a-slow")),
        "the open modal claims its own artifact"
    );
    h.press_escape(); // bare-ESC needs its disambiguation deadline
    h.turns(2);
    h.ui.rt_art_sel.set(1); // highlight the second artifact
    h.turns(2);
    h.type_text("o");
    let s = h.turns(2);
    assert!(
        s.contains("lpa.jpeg"),
        "the second artifact is on screen:\n{s}"
    );

    // The first artifact's result lost its race and must be DROPPED —
    // this is the predicate the worker consults before publishing.
    assert!(
        !h.store
            .preview_wanted(&artifact_preview_key("run-1", "a-slow")),
        "a result for the artifact left behind is no longer wanted"
    );
    assert!(
        h.store
            .preview_wanted(&artifact_preview_key("run-1", "a-wanted")),
        "the artifact actually on screen still is"
    );

    // And the modal shows its own progress line, not a stale message.
    assert!(
        s.contains("decoding image"),
        "the open preview says what IT is doing:\n{s}"
    );

    // Now the decisive half. Replay what the worker does when the slow
    // artifact finally answers — ASK first, then publish. The message
    // must never reach the screen.
    let stale = "cannot decode this image here: parse: jpeg: bad".to_string();
    let stale_key = artifact_preview_key("run-1", "a-slow");
    if h.store.preview_wanted(&stale_key) {
        h.store.artifact_text.set(Some(stale.clone()));
    }
    let s = h.turns(2);
    assert!(
        !s.contains("cannot decode this image here"),
        "the guarded publish drops a result the screen no longer wants:\n{s}"
    );
    assert!(
        s.contains("decoding image"),
        "and leaves the real one alone:\n{s}"
    );

    // Publishing WITHOUT asking is precisely the reported defect — pinned
    // here so the assertion above cannot pass for the wrong reason.
    h.store.artifact_text.set(Some(stale));
    let s = h.turns(2);
    assert!(
        s.contains("cannot decode this image here"),
        "an unguarded publish DOES paint under the wrong header — this is \
         the bug, and the guard above is what prevents it:\n{s}"
    );
}

/// The width and height of the MODAL frame on screen.
///
/// The page's own panels carry titles (`╭ Runtimes — …`), so their
/// top-left corner is followed by a space; the modal's Block is
/// untitled, so its corner is followed immediately by rule. Height
/// then runs to the `╰` in that same column. Deliberately not
/// "longest run of rule": every panel's BOTTOM border is untitled,
/// so the corner alone does not identify anything.
fn modal_frame(screen: &str) -> (usize, usize) {
    let rows: Vec<Vec<char>> = screen.lines().map(|l| l.chars().collect()).collect();
    for (top, line) in rows.iter().enumerate() {
        let Some(col) = line.windows(2).position(|w| w[0] == '╭' && w[1] == '─') else {
            continue;
        };
        let width = line[col + 1..].iter().take_while(|c| **c == '─').count() + 2;
        let height = rows[top..]
            .iter()
            .position(|r| r.get(col) == Some(&'╰'))
            .map(|d| d + 1)
            .unwrap_or(0);
        return (width, height);
    }
    (0, 0)
}

/// Operator 2026-08-20: "use more space for the previews", then "use at
/// most 66% width and 66% height". The artifact preview and the log tail
/// are the two panels whose job is to SHOW content — an image mosaic
/// gains a pair of subpixels per extra cell, a log gains a line per
/// extra row — so they scale with the terminal instead of pinning a
/// fixed 96x26 dialog, capped at two thirds of each axis.
///
/// Asserted on the modal's own frame rather than on how much text
/// happens to fit: a proportional size is a geometric claim, and a
/// fixture tuned to a knife edge would pass for the wrong reason.
#[test]
fn previews_take_two_thirds_of_each_axis() {
    use abstractgateway_console::store::{ArtifactRow, ArtifactsData};

    fn open_preview(cols: i32, rows: i32) -> String {
        let mut h = harness_sized(Size::new(cols, rows));
        h.connect_as_admin();
        h.goto_screen(4);
        h.store
            .runtimes
            .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
        h.turns(2);
        h.type_text("\r"); // choose the default plane
        h.turns(2);
        h.ui.rt_tab.set(1);
        h.ui.rt_art_modality.set("image".into());
        h.store.artifacts.set(Loadable::Ready(ArtifactsData {
            rows: vec![ArtifactRow {
                name: "lpa.jpeg".into(),
                kind: "image".into(),
                size_bytes: Some(231_500),
                run_id: "run-1".into(),
                created_at: "2026-06-13T09:57:24".into(),
                artifact_id: "a-1".into(),
                content_type: "image/jpeg".into(),
                content_path: "/tmp/store/ab/cd.bin".into(),
                workflow_id: String::new(),
                session_id: String::new(),
            }],
            total: 1,
            has_more: false,
            offset: 0,
            modality: "image".into(),
            query: String::new(),
        }));
        h.turns(2);
        h.type_text("o"); // open the highlighted artifact
        h.turns(2)
    }

    // The modal request is two thirds of the viewport; the frame drawn
    // inside it loses the Modal's own 1-cell padding on each side.
    for (cols, rows) in [(170, 40), (120, 36), (240, 60)] {
        let s = open_preview(cols, rows);
        assert!(
            s.contains("lpa.jpeg"),
            "the preview opened at {cols}x{rows}:\n{s}"
        );
        let (w, h) = modal_frame(&s);
        let want_w = (cols * 2 / 3) as usize;
        let want_h = (rows * 2 / 3) as usize;
        assert!(
            w <= want_w && w + 3 >= want_w,
            "{cols}-cell terminal: frame {w} is two thirds ({want_w}) less padding:\n{s}"
        );
        assert!(
            h <= want_h && h + 3 >= want_h,
            "{rows}-row terminal: frame {h} is two thirds ({want_h}) less padding:\n{s}"
        );
        // The cap is the POINT: a preview never covers the page.
        assert!(w < cols as usize, "the list behind stays visible:\n{s}");
    }
}

/// The log tail is the OTHER preview, and a separate call site — a
/// helper wired into one of two panels is half a fix.
#[test]
fn the_log_tail_takes_its_two_thirds_too() {
    use abstractgateway_console::store::LogFileRow;

    let (cols, rows) = (170, 40);
    let mut h = harness_sized(Size::new(cols, rows));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.type_text("\r"); // choose the default plane
    h.turns(2);
    h.ui.rt_tab.set(3);
    h.store.logs.set(Loadable::Ready(vec![LogFileRow {
        name: "abstractgateway-runner-2026-06-13.log".into(),
        home: "gateway-logs-54566bd9".into(),
        size_bytes: Some(5_700_000),
        modified_at: "2026-06-13T07:12:44".into(),
    }]));
    h.turns(2);
    h.type_text("o"); // tail the highlighted file
    let s = h.turns(2);

    assert!(
        s.contains("abstractgateway-runner-2026-06-13.log"),
        "the tail opened:\n{s}"
    );
    let (w, hgt) = modal_frame(&s);
    let (want_w, want_h) = ((cols * 2 / 3) as usize, (rows * 2 / 3) as usize);
    assert!(
        w <= want_w && w + 3 >= want_w,
        "the tail takes two thirds of the width ({w} vs {want_w}):\n{s}"
    );
    assert!(
        hgt <= want_h && hgt + 3 >= want_h,
        "and two thirds of the height ({hgt} vs {want_h}):\n{s}"
    );
}

// =======================================================================
// Models tab (screen 8): the "agentic OS" resources view
// =======================================================================

/// The tab renders the memory gauges, the resident-model table and the
/// honest degradation notes from one host-state snapshot — and the
/// degraded GPU section renders as a NOTE with its reason, never as a
/// dead gauge or a healthy blank.
#[test]
fn models_tab_renders_gauges_and_table_from_fixture() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    // Memory strip: the RAM gauge's facts and the backend-labeled
    // device gauge.
    assert!(s.contains("RAM"), "RAM gauge label:\n{s}");
    assert!(s.contains("128.0 GiB"), "RAM total humanized:\n{s}");
    assert!(s.contains("48.0 GiB free"), "RAM available:\n{s}");
    assert!(s.contains("metal"), "device gauge names its backend:\n{s}");
    assert!(s.contains("host: studio.local"), "host identity:\n{s}");
    // The GPU section is DEGRADED: a muted note with the server's
    // reason — and no utilization gauge invented for it.
    assert!(
        s.contains("gpu degraded — no GPU probe on this host"),
        "degradation renders with its reason:\n{s}"
    );
    assert!(
        !s.contains("utilization"),
        "no GPU gauge is fabricated for an unsupported host:\n{s}"
    );
    // The models table from row_v1.
    assert!(s.contains("qwen3-32b"), "model cell:\n{s}");
    assert!(s.contains("mlx"), "provider cell:\n{s}");
    assert!(s.contains("Text"), "modality label from the task:\n{s}");
    assert!(
        s.contains("8192*"),
        "calibrated context carries the star:\n{s}"
    );
    // Totals footer: resident (provider-verified) counted apart from the
    // row total — the null-resident row is a row, never "loaded".
    assert!(
        s.contains("totals: 2 resident / 3 model row(s) · 2.0 GiB · 1 session cache(s) · 4.0 KiB"),
        "totals line:\n{s}"
    );
    // The screen documents its own estimate marker.
    assert!(
        s.contains("~ = estimated size, not measured"),
        "the marker is documented in the screen title:\n{s}"
    );
}

/// LAYOUT PIN, 80x24 — the macOS default this crate already treats as
/// supported (`connection_screen_fits_at_macos_default_80x24`,
/// `runtimes_inspector_fits_at_80x24`).
///
/// The defect this pins against, measured on the shipped screen: the memory
/// strip refused to shrink and its natural height (~16 rows with the
/// itemization) took the whole block, so at 80x24, 80x26 and 110x24 the
/// Loaded table had **zero model rows** and was **unreachable** — neither
/// `Tab` nor ten `Down` presses brought it back, and the selected-row detail
/// line painted over the `╰────╯`, leaving the block with no closing corner.
/// Operator point 1 (lock/unlock on every line) was unusable at this crate's
/// own pinned default size, and the size/cache columns were invisible.
///
/// The remedy is a reservation, not a shrink: the meters and the
/// accelerator's scoped label + note are PINNED, the table's rows are
/// reserved next, and the itemization takes what is left — windowed, with an
/// affordance naming the lines that are off screen and the key that pages to
/// them. Nothing was removed and nothing was reordered.
#[test]
fn models_table_and_its_lock_verb_are_reachable_at_80x24() {
    let mut h = harness_sized(Size::new(80, 24));
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);

    // 1. THE TABLE IS THERE AT REST, with the columns the operator came for.
    let header = s
        .lines()
        .find(|l| l.contains("modality") && l.contains("provider"))
        .unwrap_or_else(|| panic!("the Loaded table header renders at 80x24:\n{s}"));
    assert!(
        header.contains("size") && header.contains("cache") && header.contains("lock"),
        "the size/cache/lock columns are visible, not scrolled off:\n{s}"
    );
    let row = s
        .lines()
        .find(|l| l.contains("qwen3-32b") && l.contains("yes"))
        .unwrap_or_else(|| panic!("at least one MODEL ROW renders at 80x24:\n{s}"));
    assert!(
        row.contains("2.0 GiB") && row.contains("\u{2298}"),
        "the row carries its size and its lock marker:\n{s}"
    );

    // 2. THE LOCK AFFORDANCE IS REACHABLE FOR A ROW — per row, and it names
    // the verb it will actually perform.
    assert!(
        s.contains("k unlocks"),
        "the selected row's lock verb renders at 80x24:\n{s}"
    );
    h.term.push_input(b"\x1b[B");
    h.turn();
    h.term.push_input(b"\x1b[B");
    let s = h.turns(2);
    assert!(
        s.contains("k locks (adopts it)"),
        "moving to the sweep row carries ITS lock verb — the table is not \
         merely visible, it is navigable:\n{s}"
    );

    // 3. THE HEAD SURVIVES THE TAIL. Ten more presses take the cursor past
    // the end of the table; the meters, the accelerator's scoped label and
    // its GGUF note must all still be on screen.
    for _ in 0..10 {
        h.term.push_input(b"\x1b[B");
        h.turn();
    }
    let s = h.turns(1);
    assert!(s.contains("RAM"), "the RAM meter is PINNED:\n{s}");
    assert!(
        s.contains("128.0 GiB"),
        "the RAM meter keeps its figures, not just its label:\n{s}"
    );
    assert!(
        s.contains("Accelerator heap · metal (all processes)"),
        "the accelerator's SCOPED label is PINNED:\n{s}"
    );
    assert!(
        s.contains("memory-mapped GGUF weights are not counted here"),
        "the GGUF note is PINNED — it is the caveat that makes the \
         accelerator figure readable:\n{s}"
    );
    assert!(
        s.lines()
            .any(|l| l.contains("qwen3-32b") && l.contains("yes")),
        "and the model rows are still there at the tail:\n{s}"
    );

    // 4. THE BOTTOM BORDER IS INTACT — one unbroken `╰───╯` run, not a
    // clipped detail line wearing the frame's row.
    let bottom = s
        .lines()
        .rev()
        .find(|l| l.trim_start().starts_with('\u{2570}'))
        .unwrap_or_else(|| panic!("the block's bottom border renders at 80x24:\n{s}"));
    assert!(
        bottom.trim_end().ends_with('\u{256F}'),
        "the bottom border closes with its corner:\n{s}"
    );
    assert!(
        bottom
            .trim()
            .chars()
            .all(|c| matches!(c, '\u{2570}' | '\u{2500}' | '\u{256F}')),
        "nothing is painted ON the bottom border:\n{s}"
    );

    // GUARD, before the paging claims below: this fixture must genuinely
    // OVERFLOW 24 rows. On a strip that happened to fit there would be
    // nothing to page and section 5 would pass for the wrong reason.
    assert!(
        s.contains("more of the memory itemization"),
        "the fixture must overflow at 80x24 — otherwise the paging \
         assertions below pin nothing:\n{s}"
    );

    // 5. THE ITEMIZATION IS REACHABLE, not merely announced. `m` pages it,
    // and one full cycle brings every line it holds on screen and returns.
    let mut seen_rss = false;
    let mut seen_sum = false;
    let mut wrapped_back = false;
    for _ in 0..14 {
        h.type_text("m");
        let s = h.turns(2);
        if s.contains("gateway process RSS") {
            seen_rss = true;
        }
        if s.contains("Σ model weights") {
            seen_sum = true;
        }
        if seen_rss && seen_sum && s.contains("host: studio.local") {
            wrapped_back = true;
        }
        // The head never leaves, at any page of the itemization.
        assert!(
            s.contains("memory-mapped GGUF weights are not counted here"),
            "the head stays pinned while the itemization pages:\n{s}"
        );
    }
    assert!(seen_rss, "`m` reaches the gateway process RSS item");
    assert!(seen_sum, "`m` reaches the Σ model weights reference");
    assert!(
        wrapped_back,
        "`m` wraps back to the top of the itemization — a bounded number of \
         presses reaches every line and returns"
    );
}

/// resident: null is the tri-state's THIRD answer and renders as its
/// own word — never collapsed into "no".
#[test]
fn models_tab_resident_null_renders_the_third_state() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    assert!(
        s.contains("mystery-model"),
        "the null-resident row renders:\n{s}"
    );
    assert!(
        s.contains("unknown"),
        "null resident renders the distinct third state:\n{s}"
    );
    let row = s
        .lines()
        .find(|l| l.contains("mystery-model"))
        .expect("mystery-model row");
    assert!(
        row.contains("unknown") && !row.contains(" no "),
        "the unknown row never reads as 'no':\n{row}"
    );
}

/// The locked row wears the ⊘ marker (U+2298, this crate's glyph law —
/// the padlock emoji is double-width and slides table columns).
#[test]
fn models_tab_locked_row_shows_the_lock_marker() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    // The TABLE row (the breakdown above also names mlx/qwen3-32b —
    // only the table row carries the tri-state residency cell).
    let row = s
        .lines()
        .find(|l| l.contains("qwen3-32b") && l.contains("mlx") && l.contains("yes"))
        .expect("locked model row renders");
    assert!(row.contains('\u{2298}'), "the locked row carries ⊘:\n{row}");
    assert!(
        !s.contains('\u{1F512}'),
        "never the padlock emoji (column-advance hazard):\n{s}"
    );
    let unknown_row = s
        .lines()
        .find(|l| l.contains("mystery-model"))
        .expect("unlocked row");
    assert!(
        !unknown_row.contains('\u{2298}'),
        "an unreported lock renders blank, never locked:\n{unknown_row}"
    );
}

/// `u` = danger confirm, defaulting to KEEP; only the explicit danger
/// choice emits the unload command — with force:false (force is the
/// 409-gated second confirm's business).
#[test]
fn models_tab_unload_confirms_then_emits_the_cmd() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    h.turns(2);
    h.type_text("u");
    let s = h.turns(2);
    assert!(
        s.contains("Unload mlx/qwen3-32b"),
        "confirm names the target:\n{s}"
    );
    assert!(
        s.contains("LOCKED"),
        "a row the snapshot says is locked forewarns the refusal:\n{s}"
    );
    // Initial highlight is keep — Enter must NOT unload.
    h.type_text("\r");
    h.turns(2);
    assert!(
        !h.drain_cmds()
            .iter()
            .any(|c| matches!(c, Cmd::UnloadModel { .. })),
        "keep does not unload"
    );
    // Again, choosing the danger option explicitly.
    h.type_text("u");
    h.turns(2);
    h.key(b"\x1b[A"); // up to "Unload"
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::UnloadModel { .. })) {
        Some(Cmd::UnloadModel {
            provider,
            model,
            force,
            ..
        }) => {
            assert_eq!(provider, "mlx");
            assert_eq!(model, "qwen3-32b");
            assert!(!force, "the first unload never forces");
        }
        other => panic!("expected UnloadModel, got {other:?}"),
    }
    // The busy entry opened at ENQUEUE: the strip names the confirmed
    // action immediately, even while a silent poll holds the lane.
    assert!(
        h.store
            .busy
            .get_untracked()
            .iter()
            .any(|op| op.label == "unloading mlx/qwen3-32b"),
        "the mutation's busy op begins at send time"
    );
}

/// The 409 model_locked hand-off: the worker fills `unload_locked`,
/// the tab's effect offers the SECOND confirm, and only the danger
/// choice emits the force unload.
#[test]
fn models_tab_locked_refusal_offers_force_unload() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    h.turns(2);
    // The worker's 409 arm posts the pair; simulate it.
    h.store
        .unload_locked
        .set(Some(("mlx".into(), "qwen3-32b".into())));
    let s = h.turns(2);
    assert!(
        s.contains("is locked") && s.contains("Force unload"),
        "the second confirm renders:\n{s}"
    );
    h.key(b"\x1b[A"); // up to the danger option
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::UnloadModel { force: true, .. })) {
        Some(Cmd::UnloadModel {
            provider, model, ..
        }) => {
            assert_eq!(provider, "mlx");
            assert_eq!(model, "qwen3-32b");
        }
        other => panic!("expected forced UnloadModel, got {other:?}"),
    }
}

/// THE METAL BUG (operator point 5): `device.allocated_bytes` is
/// PROCESS-LOCAL — it reads 0 while ~98 GB of weights are resident. The
/// gauge must show the ALL-PROCESSES pair and SAY that is what it shows;
/// a 0 B bar beside a loaded accelerator is the defect this rule kills.
/// The label is the spec's own (PART A2) and the GGUF note rides under
/// it, because this counter is blind to memory-mapped weights.
#[test]
fn models_tab_device_meter_shows_the_host_figure_not_the_process_zero() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    assert!(
        s.contains("Accelerator heap · metal (all processes)"),
        "the gauge wears the spec's exact label:\n{s}"
    );
    let meter = s
        .lines()
        .find(|l| l.contains("98.5 GiB / 107.4 GiB"))
        .expect("host_in_use / wired_limit is the meter");
    assert!(
        !meter.contains("0 B /"),
        "the process-local zero must never be the bar:\n{meter}"
    );
    assert!(
        s.contains("memory-mapped GGUF weights are not counted here"),
        "the note rides with the figure:\n{s}"
    );
    // The deleted scope word, gone from the whole screen.
    assert!(!s.contains("host-wide"), "`host-wide` is deleted:\n{s}");
}

/// Refinement 3 / SPEC PART B: under the meters, WHAT is consuming the
/// memory — the ITEMS the framework can name (one line per resident
/// model, the KV caches, the session caches, this process's RSS), then
/// behind a rule the REFERENCE counters that must never be added to
/// them. No "unattributed" remainder: it subtracted RAM-dimensioned
/// quantities from an accelerator counter and clamped the lie to 0 B.
#[test]
fn models_tab_breaks_down_what_is_consuming_memory() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    assert!(
        s.contains("consuming memory:"),
        "the breakdown header:\n{s}"
    );
    let line_of = |needle: &str| -> String {
        s.lines()
            .find(|l| l.contains(needle))
            .unwrap_or_else(|| panic!("no line for {needle}:\n{s}"))
            .to_string()
    };
    // One line per RESIDENT model, named by its `model` string, with the
    // coalesced size — the sweep row's estimate MARKED so it never reads
    // as measured — and the DETAIL naming which field supplied it.
    let swept = line_of("glm-4.6-gguf ");
    assert!(swept.contains("~93.0 GiB"), "{swept}");
    assert!(swept.contains("resident model weights"), "{swept}");
    // The non-resident / unknown-residency row is NOT itemized: only
    // what the host says it is holding consumes memory. (The Loaded
    // table below still lists it — this checks the STRIP.)
    let strip: String = s
        .lines()
        .skip_while(|l| !l.contains("consuming memory:"))
        .take_while(|l| !l.contains("Loaded"))
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !strip.contains("mystery-model"),
        "an unknown residency is not an attribution:\n{strip}"
    );
    assert!(line_of("model KV caches").contains("2.0 GiB"));
    assert!(line_of("session caches").contains("4.0 KiB"));
    assert!(line_of("gateway process RSS").contains("1.0 GiB"));
    // The references live behind their rule, and are NOT summable with
    // the items above them.
    assert!(
        strip.contains("NOT summable with the items above"),
        "{strip}"
    );
    assert!(line_of("Σ model weights").contains("~95.0 GiB"));
    assert!(line_of("RAM used").contains("80.0 GiB / 128.0 GiB"));
    assert!(
        strip.contains("Accelerator heap · metal (all processes)"),
        "the accelerator rides as a REFERENCE too:\n{strip}"
    );
    // The remainder is gone, and so is the scope word it printed.
    assert!(!s.contains("nattributed"), "no remainder line:\n{s}");
    assert!(!s.contains("host-wide"), "`host-wide` is deleted:\n{s}");
}

/// The gateway process RSS is stated EXACTLY ONCE, as the `process_rss`
/// breakdown item. The identity line used to print it a second time, and
/// one figure stated twice in one memory panel is the double-count this
/// wave exists to remove (the same duplication abstractflow's panel
/// carried; the rule is now cross-surface).
#[test]
fn models_tab_states_the_gateway_process_rss_exactly_once() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    let s = h.turns(2);
    // The strip, from the first meter down to the tables.
    let strip: Vec<&str> = s
        .lines()
        .skip_while(|l| !l.contains("RAM "))
        .take_while(|l| !l.contains("Loaded"))
        .collect();
    // 1_073_741_824 B — the fixture's RSS, and no other figure on the
    // strip renders as "1.0 GiB".
    let stated: Vec<&&str> = strip.iter().filter(|l| l.contains("1.0 GiB")).collect();
    assert_eq!(
        stated.len(),
        1,
        "the RSS figure is stated once, not twice:\n{}",
        strip.join("\n")
    );
    assert!(
        stated[0].contains("gateway process RSS"),
        "…and the one statement is the breakdown item that names what it \
         measures:\n{}",
        stated[0]
    );
    // The host identity is not a duplicate: it stays.
    assert!(
        s.contains("host: studio.local"),
        "the host id survives:\n{s}"
    );
}

/// SPEC PART B3: when the itemized weights EXCEED the accelerator heap —
/// the normal memory-mapped GGUF case, and this host's live reading —
/// the strip explains it in the spec's words, wrapped, never truncated.
#[test]
fn models_tab_names_the_gguf_case_when_weights_exceed_the_accelerator_heap() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    let mut d = host_state_fixture();
    // The live figure from the operator's Mac: 1.04 GB of accelerator
    // heap under ~93 GB of mmapped weights.
    d.device.as_mut().expect("device").host_in_use_bytes = Some(1_042_120_704);
    h.store.host_state.set(Loadable::Ready(d));
    let s = h.turns(2);
    // The note WRAPS across rows: un-wrap it and compare to the spec's
    // sentence, whole — a truncation or a rewording fails here.
    let unwrapped = s
        .lines()
        .skip_while(|l| !l.contains("Σ model weights exceeds"))
        .take(5)
        // Drop the block's own border cells before re-joining.
        .map(|l| l.trim_matches(|c: char| c.is_whitespace() || c == '│'))
        .collect::<Vec<_>>()
        .join(" ");
    assert!(
        unwrapped.contains(
            "Σ model weights exceeds the accelerator heap. That is the normal case for \
             memory-mapped GGUF weights: llama.cpp maps them from disk, so they are resident \
             as process RSS and are not counted in the accelerator heap."
        ),
        "the note renders in the spec's words, to its last one:\n{s}"
    );
}

/// Refinement 1: the lock verb is offered on EVERY resident line —
/// including the sweep row LM Studio loaded, which `POST /models/lock`
/// adopts. `k` on it emits the lock command, `lockable: null` and all.
#[test]
fn models_tab_locks_an_externally_loaded_sweep_row() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    h.turns(2);
    // Down twice: qwen3-32b → mystery-model → glm-4.6-gguf (the sweep).
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    let s = h.turns(2);
    assert!(
        s.contains("k locks (adopts it)"),
        "the row's own hint offers the adoption:\n{s}"
    );
    h.type_text("k");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::LockModel { .. })) {
        Some(Cmd::LockModel {
            provider,
            model,
            lock,
            ..
        }) => {
            assert_eq!(provider, "lmstudio");
            assert_eq!(model, "glm-4.6-gguf");
            assert!(lock, "k on an unlocked resident row LOCKS it");
        }
        other => panic!("expected LockModel, got {other:?}"),
    }
}

/// …and a row the host says it is NOT holding gets no lock and no
/// unload — with the reason named, never a silent dead key.
#[test]
fn models_tab_refuses_lock_and_unload_on_a_non_resident_row() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    let mut d = host_state_fixture();
    d.models.push(abstractgateway_console::store::ModelRow {
        provider: Some("mlx".into()),
        model: Some("cold-model".into()),
        resident: Some(false),
        ..Default::default()
    });
    h.store.host_state.set(Loadable::Ready(d));
    h.turns(2);
    for _ in 0..3 {
        h.key(b"\x1b[B");
    }
    let s = h.turns(2);
    assert!(
        s.contains("no lock ("),
        "the row's hint says the verb is not on offer:\n{s}"
    );
    h.type_text("k");
    let s = h.turns(2);
    assert!(
        !h.drain_cmds()
            .iter()
            .any(|c| matches!(c, Cmd::LockModel { .. })),
        "a non-resident row emits no lock"
    );
    assert!(
        s.contains("only a loaded model can be locked"),
        "the refusal names its reason:\n{s}"
    );
    h.type_text("u");
    let s = h.turns(2);
    assert!(
        !h.drain_cmds()
            .iter()
            .any(|c| matches!(c, Cmd::UnloadModel { .. })),
        "a non-resident row emits no unload"
    );
    assert!(
        s.contains("nothing to unload"),
        "the refusal names its reason:\n{s}"
    );
}

/// Refinement 5: `w` opens PICKERS over the gateway's own catalogs —
/// provider from `/discovery/providers`, model refreshed from the chosen
/// provider — not two free-text boxes. The lock-after-load option stays.
#[test]
fn models_tab_warmup_form_picks_provider_and_model_from_the_catalogs() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(7);
    h.store
        .host_state
        .set(Loadable::Ready(host_state_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    h.type_text("w");
    let s = h.turns(2);
    assert!(s.contains("Load (warm up) a model"), "{s}");
    assert!(
        s.contains("lock after load"),
        "the lock-after-load option survives the rework:\n{s}"
    );
    // The highlighted row prefilled the provider picker, and picking it
    // asked the gateway for THAT provider's models.
    match h.find_cmd(|c| matches!(c, Cmd::LoadModels { .. })) {
        Some(Cmd::LoadModels { provider }) => assert_eq!(provider, "mlx"),
        other => panic!("expected the provider's catalog fetch, got {other:?}"),
    }
    // The catalog lands → the model picker offers it (no typing).
    h.store.models.update(|m| {
        drop(m.insert(
            "mlx".to_string(),
            Loadable::Ready(vec!["qwen3-32b".to_string(), "qwen3-8b".to_string()]),
        ))
    });
    let s = h.turns(2);
    // Both fields are PICKERS (the ▾ chevron), and the prefilled model
    // resolved against the served catalog — nothing was typed.
    // Match the FIELD's own row (its label immediately left of the
    // picker box), not any line that happens to carry the word — the
    // memory strip behind the modal names models too.
    let field_row = |label: &str| -> Option<&str> {
        s.lines().find(|l| {
            l.contains('▾')
                && l.split('▐')
                    .next()
                    .is_some_and(|head| head.trim_end().ends_with(label))
        })
    };
    let prov = field_row("provider").expect("the provider field is a picker");
    assert!(prov.contains("mlx"), "{prov}");
    let model = field_row("model").expect("the model field is a picker over the catalog");
    assert!(model.contains("qwen3-32b"), "{model}");
    assert!(
        !s.contains("model id for that provider"),
        "no free-text lane once the catalog answered:\n{s}"
    );
}

/// Digit 8 jumps to the Models tab in browse mode — APPEND-SAFE: the
/// tab rides the end of the lockstep arrays, so the existing digit
/// tests (4 → users at :406) keep passing untouched.
#[test]
fn digit_8_jumps_to_the_models_tab() {
    let mut h = harness();
    h.connect_as_admin();
    h.goto_screen(1);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    h.store.providers.set(Loadable::Ready(providers_fixture()));
    h.turns(2);
    h.type_text("8");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 7, "digit 8 → screen index 7");
    // Entering the tab arms the generation-gated poll chain: exactly
    // one first poll, under the CURRENT generation.
    let gen_now = h.store.host_poll_gen.get_untracked();
    let polls: Vec<Cmd> = h
        .drain_cmds()
        .into_iter()
        .filter(|c| matches!(c, Cmd::PollHostState { .. }))
        .collect();
    match polls.as_slice() {
        [Cmd::PollHostState { gen, first }] => {
            assert_eq!(*gen, gen_now, "the poll rides the live generation");
            assert!(*first, "first hop shows the busy label");
        }
        other => panic!("expected exactly one PollHostState, got {other:?}"),
    }
    // Leaving the tab bumps the generation — the chain's next result
    // (and its reschedule) dies on the worker's UI-thread gate.
    h.type_text("4");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 3, "digit 4 still → users");
    assert!(
        h.store.host_poll_gen.get_untracked() > gen_now,
        "tab exit bumps the poll generation"
    );
}

/// The footer hint arms must stay in LOCKSTEP with the screen array:
/// the Workflows wave drifted them once (Review's sandbox hints
/// rendered on the Workflows screen; Review showed none). One
/// distinctive per-screen verb each, on a terminal wide enough that
/// right-edge truncation can't eat the evidence.
#[test]
fn footer_hints_stay_in_lockstep_with_screens() {
    let mut h = harness_sized(Size::new(150, 40));
    h.connect_as_admin();
    h.ui.wizard.set(false);
    for (screen, needle) in [
        (1usize, "add connection"),
        (2, "edit route"),
        (3, "rotate token"),
        (4, "inspect runtime"),
        (5, "drafts"),
        (6, "run the test"),
        (7, "context estimate"),
        // The shared screens' own verbs (abstractcore-console HINTS).
        (8, "fits only"),
        (9, "open download page"),
    ] {
        h.ui.screen.set(screen);
        let s = h.turns(2);
        assert!(
            s.contains(needle),
            "screen {screen} footer hints carry '{needle}':\n{s}"
        );
    }
    // The drift this pins against: the sandbox verb must NOT render on
    // the Workflows screen (arm 5 once wore Review's hints).
    h.ui.screen.set(5);
    let s = h.turns(2);
    assert!(
        !s.contains("run the test"),
        "workflows screen never wears Review's sandbox hints:\n{s}"
    );
}

// ---- AbstractCore's shared Models (9) / Engines (0) screens -------------
//
// The screens are the published `abstractcore-console` crate's; what is
// pinned here is the GATEWAY's wiring of them: tabs 9/0, the `0` root
// key, the shared notice lane (toasts), the footer, the reconnect reset,
// the `q` refusal, and the host label the confirms print. The fixtures
// are AbstractCore's contract documents (tests/fixtures, copied from
// the crate's own suite).

fn fixture(name: &str) -> Value {
    let text = match name {
        "host_profile" => include_str!("fixtures/host_profile.json"),
        "engines_status" => include_str!("fixtures/engines_status.json"),
        "model_catalog" => include_str!("fixtures/model_catalog.json"),
        "models_installed" => include_str!("fixtures/models_installed.json"),
        "job_running" => include_str!("fixtures/job_running.json"),
        "job_completed" => include_str!("fixtures/job_completed.json"),
        other => panic!("no fixture {other}"),
    };
    serde_json::from_str(text).expect("fixture parses")
}

/// The contract backend in memory: fixtures for every read (the
/// catalog filtered the way the gateway filters it), a call log, and
/// jobs walked through `polls`.
#[derive(Default)]
struct MockTransport {
    calls: Mutex<Vec<String>>,
    /// Successive `job()` answers; empty = the last job, completed.
    polls: Mutex<VecDeque<Value>>,
    last_job: Mutex<Option<Value>>,
    cancelled: Mutex<bool>,
    /// When set, `delete_model` refuses with this error.
    refuse_delete: Mutex<Option<TransportError>>,
}

impl MockTransport {
    fn record(&self, call: String) {
        self.calls.lock().unwrap().push(call);
    }

    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }

    fn called(&self, prefix: &str) -> bool {
        self.calls().iter().any(|c| c.starts_with(prefix))
    }

    fn count(&self, prefix: &str) -> usize {
        self.calls()
            .iter()
            .filter(|c| c.starts_with(prefix))
            .count()
    }

    fn job_doc(kind: &str, status: &str, extra: Value) -> Value {
        let mut j = fixture("job_running");
        j["kind"] = json!(kind);
        j["status"] = json!(status);
        j["job_id"] = json!(format!("{kind}_1"));
        if kind != "download" {
            j["log_tail"] = json!([]);
            j["downloaded_bytes"] = Value::Null;
            j["total_bytes"] = Value::Null;
        }
        if let Value::Object(m) = extra {
            for (k, v) in m {
                j[k] = v;
            }
        }
        j
    }

    fn started(&self, doc: Value) -> Result<Value, TransportError> {
        *self.last_job.lock().unwrap() = Some(doc.clone());
        Ok(doc)
    }
}

impl ConsoleTransport for MockTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        self.record("host_profile".into());
        Ok(fixture("host_profile"))
    }
    fn engines_status(&self, probe: bool) -> Result<Value, TransportError> {
        self.record(format!("engines_status probe={probe}"));
        Ok(fixture("engines_status"))
    }
    fn models_catalog(
        &self,
        q: &str,
        engine: Option<&str>,
        fits_only: bool,
    ) -> Result<Value, TransportError> {
        self.record(format!(
            "catalog q={q} engine={} fits={fits_only}",
            engine.unwrap_or("-")
        ));
        let mut c = fixture("model_catalog");
        let q = q.to_lowercase();
        let rows: Vec<Value> = c["rows"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| q.is_empty() || r["id"].as_str().unwrap().contains(&q))
            .map(|r| {
                let mut r = r.clone();
                let arts: Vec<Value> = r["artifacts"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|a| engine.is_none_or(|e| a["provider"] == e))
                    .filter(|a| {
                        !fits_only || matches!(a["fit"]["verdict"].as_str(), Some("fits" | "tight"))
                    })
                    .cloned()
                    .collect();
                r["artifacts"] = json!(arts);
                r
            })
            .collect();
        c["rows"] = json!(rows);
        Ok(c)
    }
    fn models_installed(&self, provider: Option<&str>) -> Result<Value, TransportError> {
        self.record(format!("installed provider={}", provider.unwrap_or("-")));
        Ok(fixture("models_installed"))
    }
    fn start_download(&self, provider: &str, artifact: &str) -> Result<Value, TransportError> {
        self.record(format!("download {provider} {artifact}"));
        self.started(Self::job_doc(
            "download",
            "running",
            json!({"provider": provider, "artifact": artifact, "percent": 0.0,
                   "downloaded_bytes": 0, "message": "pulling manifest"}),
        ))
    }
    fn delete_model(
        &self,
        provider: &str,
        artifact: &str,
        force: bool,
    ) -> Result<Value, TransportError> {
        self.record(format!("delete {provider} {artifact} force={force}"));
        if let Some(e) = self.refuse_delete.lock().unwrap().clone() {
            return Err(e);
        }
        self.started(Self::job_doc(
            "delete",
            "completed",
            json!({"provider": provider, "artifact": artifact, "percent": 100.0,
                   "command": ["lms", "rm", artifact]}),
        ))
    }
    fn engine_install(&self, id: &str, dry_run: bool) -> Result<Value, TransportError> {
        self.record(format!("install {id} dry_run={dry_run}"));
        let command = json!(["brew", "install", id]);
        if dry_run {
            return self.started(Self::job_doc(
                "engine_install",
                "completed",
                json!({"provider": null, "artifact": null, "engine": id, "dry_run": true,
                       "command": command, "percent": null}),
            ));
        }
        self.started(Self::job_doc(
            "engine_install",
            "running",
            json!({"provider": null, "artifact": null, "engine": id, "command": command,
                   "percent": null, "message": "==> Downloading ollama"}),
        ))
    }
    fn job(&self, id: &str) -> Result<Value, TransportError> {
        self.record(format!("job {id}"));
        let last = self
            .last_job
            .lock()
            .unwrap()
            .clone()
            .expect("a job started");
        if *self.cancelled.lock().unwrap() {
            let mut j = last;
            j["status"] = json!("cancelled");
            return Ok(j);
        }
        if let Some(next) = self.polls.lock().unwrap().pop_front() {
            return Ok(next);
        }
        let mut j = last;
        j["status"] = json!("completed");
        j["percent"] = json!(100.0);
        Ok(j)
    }
    fn cancel_job(&self, id: &str) -> Result<Value, TransportError> {
        self.record(format!("cancel {id}"));
        *self.cancelled.lock().unwrap() = true;
        let mut j = self
            .last_job
            .lock()
            .unwrap()
            .clone()
            .expect("a job started");
        j["status"] = json!("cancelled");
        Ok(j)
    }
    /// What the gateway transport says (transport_http::label_for).
    fn host_label(&self) -> String {
        "gateway host studio (10.0.0.5:8080)".into()
    }
}

impl Harness {
    /// Turn frames until `pred` holds on the screen text (the screens'
    /// worker answers asynchronously); panics with the last frame.
    fn settle_until(&mut self, what: &str, pred: impl Fn(&str) -> bool) -> String {
        let mut last = String::new();
        for _ in 0..400 {
            last = self.turn();
            if pred(&last) {
                return last;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("never saw {what}:\n{last}\ncalls: {:?}", self.mock.calls());
    }

    fn settle_until_contains(&mut self, needle: &str) -> String {
        let n = needle.to_string();
        self.settle_until(needle, move |s| s.contains(&n))
    }

    fn wait_for_call(&mut self, what: &str, pred: impl Fn(&[String]) -> bool) {
        for _ in 0..400 {
            if pred(&self.mock.calls()) {
                return;
            }
            self.turn();
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("never saw {what}: {:?}", self.mock.calls());
    }

    /// Connected, browse mode, on the Workflows screen (the Connection
    /// screen's URL field owns the keyboard — digits would be typed).
    fn browse_connected(&mut self) {
        self.connect_as_admin();
        self.ui.wizard.set(false);
        self.ui.screen.set(ui::SCREEN_WORKFLOWS);
        self.turns(2);
    }

    fn open_models(&mut self) -> String {
        self.key(b"9");
        self.settle_until("the catalog rows", |s| {
            s.contains("Qwen3 8B") && s.contains("Apple M5 Max")
        })
    }

    fn open_engines(&mut self) -> String {
        self.key(b"0");
        self.settle_until("the engines table", |s| {
            s.contains("Ollama") && s.contains("llama.cpp")
        })
    }

    fn select_artifact(&mut self, artifact: &str) -> String {
        let idx = self
            .screens
            .catalog
            .with_untracked(|c| {
                c.ready()
                    .and_then(|d| d.rows.iter().position(|r| r.artifact == artifact))
            })
            .unwrap_or_else(|| panic!("{artifact} is not in the catalog fixture"));
        self.screens.catalog_sel.set(idx);
        self.turns(2)
    }

    fn select_engine(&mut self, id: &str) -> String {
        let idx = self
            .screens
            .engines
            .with_untracked(|e| {
                e.ready()
                    .and_then(|d| d.engines.iter().position(|r| r.id == id))
            })
            .unwrap_or_else(|| panic!("{id} is not in the engines fixture"));
        self.screens.engine_sel.set(idx);
        self.turns(2)
    }
}

#[test]
fn models_tab_9_renders_the_shared_catalog_once() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    let s = h.open_models();
    assert!(s.contains("9 Models"), "tab 9 is Models:\n{s}");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_CATALOG);
    // Contract G vocabulary, rendered by the shared screen.
    for word in ["fits", "too large", "not downloaded", "installed"] {
        assert!(s.contains(word), "{word} rendered:\n{s}");
    }
    assert!(
        s.contains("ollama: unreachable"),
        "installed errors shown:\n{s}"
    );
    // The gateway footer: the new digit range + the screen's own verbs.
    assert!(s.contains("1-9,0"), "{s}");
    assert!(s.contains("download") && s.contains("filter"), "{s}");
    // Entering read host, catalog and installed ONCE each — the gateway's
    // connected-entry effect and the screen's mount effect must not
    // double-send.
    assert_eq!(h.mock.count("catalog"), 1, "{:?}", h.mock.calls());
    assert_eq!(h.mock.count("host_profile"), 1, "{:?}", h.mock.calls());
    assert_eq!(h.mock.count("installed"), 1, "{:?}", h.mock.calls());
    // Leave and come back: no re-read. `r` re-reads.
    h.key(b"6");
    h.turns(3);
    h.key(b"9");
    h.turns(3);
    assert_eq!(h.mock.count("catalog"), 1);
    h.key(b"r");
    h.wait_for_call("a second catalog read", |calls| {
        calls.iter().filter(|c| c.starts_with("catalog")).count() == 2
    });
    h.settle_until_contains("Qwen3 8B");
    // `r` on the shared screen is ITS refresh, never a gateway command.
    assert!(
        h.drain_cmds()
            .iter()
            .all(|c| !matches!(c, Cmd::PollHostState { .. })),
        "no gateway-lane reload for the shared screen"
    );
}

#[test]
fn zero_jumps_to_engines_in_browse_and_is_refused_in_the_wizard() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    let s = h.open_engines();
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_ENGINES);
    assert!(s.contains("0 Engines"), "{s}");
    assert!(
        s.contains("gateway host studio"),
        "the engines screen names the gateway host:\n{s}"
    );
    assert!(h.mock.called("engines_status probe=false"));
    assert!(s.contains("open download page"), "engines footer:\n{s}");
    // Wizard: digits are refused WITH a reason, 0 included.
    h.ui.wizard.set(true);
    h.ui.screen.set(1);
    h.turns(2);
    h.key(b"0");
    let s = h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), 1, "wizard does not jump");
    assert!(s.contains("digit jumps work in browse mode"), "{s}");
}

#[test]
fn wizard_walks_on_to_models_and_engines_with_their_goals() {
    let mut h = harness_sized(Size::new(150, 40));
    h.connect_as_admin();
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_CATALOG);
    let s = h.settle_until_contains("Qwen3 8B");
    assert!(
        s.contains("Step goal:") && s.contains("w downloads a model"),
        "{s}"
    );
    h.ui.screen.set(ui::SCREEN_ENGINES);
    let s = h.settle_until_contains("llama.cpp");
    assert!(s.contains("i installs a local engine"), "{s}");
}

#[test]
fn models_filter_slash_requeries_through_the_transport() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_models();
    h.key(b"/");
    let s = h.turns(2);
    assert!(s.contains("Filter models"), "the filter input opens:\n{s}");
    h.type_text("gemma");
    h.turns(1);
    h.key(b"\r");
    let s = h.settle_until("only gemma", |s| {
        s.contains("Gemma 3 27B") && !s.contains("Qwen3 8B")
    });
    assert!(s.contains("filter \"gemma\""), "{s}");
    assert!(h.mock.called("catalog q=gemma engine=- fits=false"));
    h.key(b"f");
    h.wait_for_call("the fits-only re-query", |c| {
        c.iter().any(|c| c == "catalog q=gemma engine=- fits=true")
    });
}

#[test]
fn download_progress_reaches_the_gateway_toast_lane() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_models();
    h.mock
        .polls
        .lock()
        .unwrap()
        .extend([fixture("job_running"), fixture("job_running")]);
    h.select_artifact("qwen3:8b");
    h.key(b"w");
    let s = h.settle_until("the job at 42%", |s| s.contains("42%"));
    assert!(s.contains("download ollama qwen3:8b"), "{s}");
    assert!(s.contains("c cancels"), "{s}");
    // The outcome lands on the GATEWAY's notice signal (shared lane):
    // the footer mirrors it and the toast effect shows it.
    h.settle_until("the completion notice", |s| {
        s.contains("✓ download ollama qwen3:8b completed")
    });
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("download ollama qwen3:8b completed"),
        "the gateway notice carries it: {notice}"
    );
    assert!(!h.screens.job_active());
}

#[test]
fn delete_refusal_shows_the_gateways_blockers() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_models();
    h.key(b"v");
    h.settle_until_contains("3 installed");
    let idx = h
        .screens
        .installed
        .with_untracked(|i| {
            i.ready()
                .and_then(|d| d.rows.iter().position(|r| r.provider == "mlx"))
        })
        .unwrap();
    h.screens.installed_sel.set(idx);
    h.turns(2);
    h.key(b"d");
    let s = h.turns(2);
    assert!(
        s.contains("loaded in memory right now"),
        "blocker spelled out:\n{s}"
    );
    assert!(
        s.contains("gateway host studio"),
        "the delete names where it runs:\n{s}"
    );
    // The gateway refuses (409 + body) — what HttpTransport hands over.
    *h.mock.refuse_delete.lock().unwrap() = Some(TransportError::refused(
        "model is loaded",
        Some(json!({"ok": false, "status": "refused", "delete_blockers": ["loaded"]})),
    ));
    h.key(b"1");
    h.turns(1);
    h.key(b"\r");
    let s = h.settle_until_contains("refused: model is loaded");
    assert!(s.contains("(loaded)"), "the blockers ride along:\n{s}");
    assert!(h
        .mock
        .called("delete mlx mlx-community/gpt-oss-20b-4bit force=true"));
}

#[test]
fn install_confirm_shows_argv_and_the_gateway_host() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_engines();
    h.select_engine("ollama");
    h.key(b"i");
    let s = h.turns(2);
    assert!(
        s.contains("Install Ollama on gateway host studio (10.0.0.5:8080)?"),
        "{s}"
    );
    assert!(
        s.contains("command   brew install ollama"),
        "the exact argv:\n{s}"
    );
    assert!(s.contains("runs on   gateway host studio"), "{s}");
    assert!(s.contains("Dry run"), "{s}");
    // Default = cancel; nothing is sent.
    h.key(b"\r");
    h.settle_until_contains("install cancelled — nothing ran");
    assert!(!h.mock.called("install"));
    // Dry run asks the gateway, runs nothing.
    h.key(b"i");
    h.turns(2);
    h.key(b"2");
    h.turns(1);
    h.key(b"\r");
    h.settle_until_contains("dry run: install ollama would run `brew install ollama`");
    assert!(h.mock.called("install ollama dry_run=true"));
}

#[test]
fn a_running_install_blocks_q_and_c_cancels_it() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_engines();
    h.mock.polls.lock().unwrap().extend((0..400).map(|_| {
        MockTransport::job_doc(
            "engine_install",
            "running",
            json!({"engine": "ollama", "provider": null, "artifact": null,
                   "percent": null, "message": "==> Downloading ollama"}),
        )
    }));
    h.select_engine("ollama");
    h.key(b"i");
    h.turns(2);
    h.key(b"1");
    h.turns(1);
    h.key(b"\r");
    h.settle_until_contains("Downloading ollama");
    assert!(h.screens.job_active());
    // Browse-mode q refuses while the gateway job runs.
    h.key(b"q");
    h.settle_until_contains("models/engines job is running on the gateway");
    h.key(b"c");
    let s = h.settle_until_contains("⊘ install ollama cancelled");
    assert!(s.contains("cancelled"), "{s}");
    assert!(h.mock.called("cancel engine_install_1"));
    assert!(!h.screens.job_active());
}

#[test]
fn a_reconnect_forgets_the_old_gateways_models_and_reloads() {
    use abstractcore_console::screens::Remote;
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_models();
    assert_eq!(h.mock.count("catalog"), 1);
    // The worker's probe path: Probing (+ Store::reset_domains).
    h.store.conn.set(ConnPhase::Probing);
    h.turns(2);
    assert!(
        h.screens.catalog.with_untracked(Remote::is_not_asked)
            && h.screens.installed.with_untracked(Remote::is_not_asked)
            && h.screens.host.with_untracked(Remote::is_not_asked),
        "Probing resets the shared screens' reads"
    );
    // No read while not connected.
    h.turns(3);
    assert_eq!(h.mock.count("catalog"), 1, "{:?}", h.mock.calls());
    // Connected again, still on the Models tab: it reloads by itself.
    h.connect_as_admin();
    h.wait_for_call("the post-reconnect catalog read", |c| {
        c.iter().filter(|c| c.starts_with("catalog")).count() == 2
    });
    h.settle_until_contains("Qwen3 8B");
    // The UI-side reset (Connect button) clears them too.
    h.screens.engines.set(Remote::Loading);
    let ctx_reset = h.screens;
    abstractgateway_console::ui::reset_screens(&ctx_reset);
    assert!(h.screens.engines.with_untracked(Remote::is_not_asked));
}

// =======================================================================
// Network exposure panel (Connection screen, gateway_network_v1)
// =======================================================================

fn network_fixture(configured: &str, running_bind: &str, restart: bool, applies: bool) -> Value {
    json!({
        "schema": "gateway_network_v1",
        "writable": true,
        "configured": {"mode": configured, "label": match configured {
            "localhost" => "Localhost only", "lan" => "Local network", _ => "Internet"},
            "port": 8080, "bind_host": if configured == "localhost" {"127.0.0.1"} else {"0.0.0.0"},
            "source": "stored", "port_source": "stored", "internet_acknowledged": null},
        "effective": {"mode": if running_bind == "127.0.0.1" {"localhost"} else {"lan"},
            "label": if running_bind == "127.0.0.1" {"Localhost only"} else {"Local network"},
            "bind_host": running_bind, "port": 8080, "overridden_by_cli": !applies,
            "host_source": if applies {"setting"} else {"cli"}, "port_source": "setting", "running": true},
        "restart_required": restart,
        "restart": {"available": true, "applies": applies, "needed": restart,
            "how": "POST /api/gateway/network/restart (admin)",
            "reason": if applies { Value::Null } else { json!("this gateway was started with --host/--port on its command line") }},
        "auth": {"user_auth": true, "token_auth": false, "ok_for_mode": true, "will_enable_user_auth": false},
        "modes": [
            {"id": "localhost", "label": "Localhost only", "selected": configured == "localhost", "allowed": true, "requires_acknowledgement": false},
            {"id": "lan", "label": "Local network", "selected": configured == "lan", "allowed": true, "requires_acknowledgement": false},
            {"id": "internet", "label": "Internet", "selected": configured == "internet", "allowed": true, "requires_acknowledgement": true}
        ],
        "addresses": [
            {"kind": "loopback", "url": "http://127.0.0.1:8080", "host": "127.0.0.1", "port": 8080, "reachable": true, "note": "this machine only"},
            {"kind": "lan", "url": "http://192.168.1.23:8080", "host": "192.168.1.23", "port": 8080, "interface": "en0", "interface_label": "Wi-Fi", "reachable": running_bind == "0.0.0.0"},
            {"kind": "hostname", "url": "http://mymac.local:8080", "host": "mymac.local", "port": 8080, "reachable": running_bind == "0.0.0.0"}
        ],
        "copy_hint": if running_bind == "0.0.0.0" {"http://192.168.1.23:8080"} else {"http://127.0.0.1:8080"},
        "warnings": []
    })
}

fn set_network(h: &mut Harness, v: Value) -> String {
    h.store.network.set(Loadable::Ready(
        abstractgateway_console::store::NetworkData::from_value(&v),
    ));
    h.turns(3)
}

/// Focus the mode picker: Tab past URL, token and the probe button.
fn focus_network_modes(h: &mut Harness) {
    for _ in 0..3 {
        h.key(b"\t");
        h.turn();
    }
}

#[test]
fn network_panel_loads_on_connect_and_renders_modes_and_addresses() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    let s = h.turns(3);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::LoadNetwork)).is_some(),
        "connected → the panel asks for GET /network"
    );
    assert!(s.contains("reading network exposure"), "loading line:\n{s}");
    let s = set_network(
        &mut h,
        network_fixture("localhost", "127.0.0.1", false, true),
    );
    for needle in [
        "Network exposure",
        "Localhost only",
        "Local network",
        "Internet…",
        "Network exposure: Localhost only · port 8080 · running: Localhost only 127.0.0.1:8080",
        "● http://127.0.0.1:8080",
        "○ http://192.168.1.23:8080",
        "lan · Wi-Fi",
        "http://mymac.local:8080",
        "primary http://127.0.0.1:8080",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    assert!(
        !s.contains("restart required"),
        "no banner when in sync:\n{s}"
    );
    // The text render is the report's evidence (MISSION R).
    if let Ok(dir) = std::env::var("MISSION_R_RENDER_DIR") {
        std::fs::write(format!("{dir}/tui_connection_localhost.txt"), &s).expect("write render");
    }
}

#[test]
fn network_panel_restart_banner_and_cli_override_story() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    let s = set_network(&mut h, network_fixture("lan", "127.0.0.1", true, true));
    assert!(
        s.contains("restart required to apply 'lan' on port 8080"),
        "banner:\n{s}"
    );
    assert!(s.contains("Restart to apply"), "restart button:\n{s}");
    if let Ok(dir) = std::env::var("MISSION_R_RENDER_DIR") {
        std::fs::write(format!("{dir}/tui_connection_restart_required.txt"), &s)
            .expect("write render");
    }
    let s = set_network(&mut h, network_fixture("lan", "127.0.0.1", true, false));
    assert!(
        s.contains("not applied (command line overrides the setting): this gateway was started with --host/--port"),
        "cli story:\n{s}"
    );
    assert!(
        !s.contains("Restart to apply"),
        "no restart button that cannot apply:\n{s}"
    );
}

#[test]
fn network_mode_arrow_enter_posts_the_chosen_mode() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(
        &mut h,
        network_fixture("localhost", "127.0.0.1", false, true),
    );
    h.drain_cmds();
    focus_network_modes(&mut h);
    h.key(b"\x1b[B"); // Down → Local network
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. })) {
        Some(Cmd::SetNetwork {
            mode,
            acknowledge_internet,
        }) => {
            assert_eq!(mode, "lan");
            assert!(!acknowledge_internet);
        }
        other => panic!("expected SetNetwork lan, got {other:?}"),
    }
}

#[test]
fn network_internet_needs_the_danger_confirm_then_acknowledges() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(
        &mut h,
        network_fixture("localhost", "127.0.0.1", false, true),
    );
    h.drain_cmds();
    focus_network_modes(&mut h);
    h.key(b"\x1b[B");
    h.turn();
    h.key(b"\x1b[B"); // Internet…
    h.turn();
    h.type_text("\r");
    let s = h.turns(2);
    assert!(
        s.contains("Expose the gateway to the internet?"),
        "confirm:\n{s}"
    );
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. }))
            .is_none(),
        "nothing posted before the confirm"
    );
    h.type_text("\r"); // initial highlight is "keep"
    h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. }))
            .is_none(),
        "keep posts nothing"
    );
    h.type_text("\r"); // the radio still holds Internet: Enter asks again
    h.turns(2);
    h.key(b"\x1b[A"); // up to the danger option
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. })) {
        Some(Cmd::SetNetwork {
            mode,
            acknowledge_internet,
        }) => {
            assert_eq!(mode, "internet");
            assert!(acknowledge_internet, "the confirm IS the acknowledgement");
        }
        other => panic!("expected SetNetwork internet, got {other:?}"),
    }
}

#[test]
fn network_refused_mode_says_the_fix_and_posts_nothing() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    let mut v = network_fixture("localhost", "127.0.0.1", false, true);
    v["modes"][1]["allowed"] = json!(false);
    v["modes"][1]["reason"] = json!("'lan' requires user auth");
    v["modes"][1]["fix"] = json!("started with accounts off");
    let s = set_network(&mut h, v);
    assert!(s.contains("Local network (needs user auth)"), "label:\n{s}");
    h.drain_cmds();
    focus_network_modes(&mut h);
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r");
    let s = h.turns(2);
    assert!(
        h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. }))
            .is_none(),
        "a refused mode posts nothing"
    );
    assert!(s.contains("fix: started with accounts off"), "fix shown:\n{s}");
}

#[test]
fn network_c_copies_the_highlighted_address() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(&mut h, network_fixture("lan", "0.0.0.0", false, true));
    focus_network_modes(&mut h);
    h.key(b"\t"); // → address list
    h.turn();
    h.key(b"\x1b[B"); // → the Wi-Fi LAN URL
    h.turn();
    h.type_text("c");
    let s = h.turns(2);
    assert!(
        s.contains("copied http://192.168.1.23:8080"),
        "copy notice:\n{s}"
    );
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("copied http://192.168.1.23:8080")
    );
    // Enter on a row copies too (List activation).
    h.key(b"\x1b[B");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("copied http://mymac.local:8080")
    );
}

#[test]
fn network_c_in_the_url_field_still_types() {
    let mut h = harness_sized(Size::new(110, 40));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(&mut h, network_fixture("lan", "0.0.0.0", false, true));
    h.store.notice.set(None);
    h.ui.conn_url.set(String::new());
    h.turn();
    h.type_text("c");
    h.turns(2);
    assert_eq!(
        h.ui.conn_url.get_untracked(),
        "c",
        "the URL field keeps its letters"
    );
    assert!(
        !h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .starts_with("copied"),
        "no copy from the URL field"
    );
}


// =======================================================================
// Reverse proxy (mission Z): allowed origins + trust proxy, same door as
// the mode (POST /network), the gateway's words on refusal.
// =======================================================================

fn proxy_fixture(origins: &[&str], trust: bool, env_origins: Option<&[&str]>, env_trust: Option<bool>) -> Value {
    let mut v = network_fixture("internet", "0.0.0.0", false, true);
    let mut o = json!({
        "value": origins, "source": if origins.is_empty() {"default"} else {"setting"},
        "overridden_by_env": env_origins.is_some(),
        "effective": ["http://localhost:*", "http://127.0.0.1:*"],
        "builtin": ["http://localhost:*", "http://127.0.0.1:*"], "self_origins": [],
        "applies": "live", "warnings": []
    });
    if let Some(e) = env_origins {
        o["source"] = json!("env");
        o["env_name"] = json!("ABSTRACTGATEWAY_ALLOWED_ORIGINS");
        o["env_value"] = json!(e);
    }
    let mut t = json!({"value": trust, "source": if trust {"setting"} else {"default"},
        "overridden_by_env": env_trust.is_some(), "effective": env_trust.unwrap_or(trust), "applies": "live"});
    if env_trust.is_some() {
        t["source"] = json!("env");
        t["env_name"] = json!("ABSTRACTGATEWAY_TRUST_PROXY");
    }
    v["reverse_proxy"] = json!({"allowed_origins": o, "trust_proxy": t});
    v
}

/// Tab from the URL field to the origins edit line (URL, token, probe,
/// modes, addresses, origins).
fn focus_origins_line(h: &mut Harness) {
    for _ in 0..5 {
        h.key(b"\t");
        h.turn();
    }
}

#[test]
fn network_reverse_proxy_shows_values_and_where_they_come_from() {
    let mut h = harness_sized(Size::new(160, 60));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    let s = set_network(&mut h, proxy_fixture(&["https://gateway.example.com"], false, None, None));
    for needle in [
        "Reverse proxy",
        "origins: https://gateway.example.com [saved setting]",
        "trust proxy: off [default]",
        "applies to the next request",
        "Enter saves · empty clears",
        "trust the proxy's client address (X-Forwarded-For): only when your own proxy sits in front",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    assert!(!s.contains("environment override"), "no override:\n{s}");
    if let Ok(dir) = std::env::var("MISSION_Z_RENDER_DIR") {
        std::fs::write(format!("{dir}/tui_network_reverse_proxy.txt"), &s).expect("write render");
    }
    // The environment override is said in words, per field.
    let s = set_network(
        &mut h,
        proxy_fixture(&["https://gateway.example.com"], false, Some(&["https://pinned.example"]), Some(true)),
    );
    assert!(s.contains("[environment override]"), "override tag:\n{s}");
    assert!(
        s.contains("this gateway was started with ABSTRACTGATEWAY_ALLOWED_ORIGINS in its environment: that list decides (https://pinned.example)"),
        "origins override line:\n{s}"
    );
    assert!(
        s.contains("this gateway was started with ABSTRACTGATEWAY_TRUST_PROXY in its environment: trust proxy is on"),
        "trust override line:\n{s}"
    );
    assert!(!s.contains("set ABSTRACTGATEWAY"), "never an env instruction:\n{s}");
    if let Ok(dir) = std::env::var("MISSION_Z_RENDER_DIR") {
        std::fs::write(format!("{dir}/tui_network_reverse_proxy_env_override.txt"), &s).expect("write render");
    }
}

#[test]
fn network_origins_line_enter_saves_the_whole_list() {
    let mut h = harness_sized(Size::new(160, 60));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(&mut h, proxy_fixture(&["https://a.example"], false, None, None));
    h.drain_cmds();
    focus_origins_line(&mut h);
    h.key(b"\x1b[F"); // End
    h.turn();
    h.type_text(", https://b.example:8443 ,");
    h.turn();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SetNetworkProxy { .. })) {
        Some(Cmd::SetNetworkProxy { allowed_origins, trust_proxy }) => {
            assert_eq!(
                allowed_origins,
                Some(vec!["https://a.example".to_string(), "https://b.example:8443".to_string()])
            );
            assert_eq!(trust_proxy, None, "only the origins change");
        }
        other => panic!("expected SetNetworkProxy origins, got {other:?}"),
    }
    assert!(h.find_cmd(|c| matches!(c, Cmd::SetNetwork { .. })).is_none(), "the mode is untouched");
}

#[test]
fn network_trust_checkbox_saves_on_toggle() {
    let mut h = harness_sized(Size::new(160, 60));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    set_network(&mut h, proxy_fixture(&[], false, None, None));
    h.drain_cmds();
    focus_origins_line(&mut h);
    h.key(b"\t"); // → the checkbox
    h.turn();
    h.type_text(" ");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SetNetworkProxy { .. })) {
        Some(Cmd::SetNetworkProxy { allowed_origins, trust_proxy }) => {
            assert_eq!(trust_proxy, Some(true));
            assert_eq!(allowed_origins, None, "only trust changes");
        }
        other => panic!("expected SetNetworkProxy trust, got {other:?}"),
    }
}

#[test]
fn network_reverse_proxy_is_read_only_without_admin() {
    let mut h = harness_sized(Size::new(160, 60));
    h.connect_as_admin();
    h.ui.screen.set(0);
    h.turns(2);
    let mut v = proxy_fixture(&["https://a.example"], true, None, None);
    v["writable"] = json!(false);
    let s = set_network(&mut h, v);
    assert!(s.contains("changing the reverse proxy needs an admin token"), "read-only:\n{s}");
    assert!(!s.contains("Enter saves"), "no edit line:\n{s}");
}

#[test]
fn network_proxy_body_and_notes_use_the_gateways_words() {
    use abstractgateway_console::api::{ApiError, ApiErrorKind};
    use abstractgateway_console::ui::network::parse_origins_line;
    use abstractgateway_console::worker::{network_proxy_body, network_proxy_note};

    assert_eq!(parse_origins_line(" https://a.example, ,https://b.example "), vec!["https://a.example", "https://b.example"]);
    assert_eq!(parse_origins_line(""), Vec::<String>::new());
    assert_eq!(network_proxy_body(&Some(vec![]), None), json!({"allowed_origins": []}));
    assert_eq!(network_proxy_body(&None, Some(false)), json!({"trust_proxy": false}));
    let refused = "1 origin is not valid (nothing was saved): https://x.example/: no trailing slash: an origin is scheme://host[:port] (write https://x.example)";
    let err = ApiError {
        kind: ApiErrorKind::Http(400),
        message: "bad".into(),
        body: Some(json!({"ok": false, "reason_code": "invalid_origins", "refused_reason": refused})),
        timed_out: false,
    };
    assert_eq!(network_proxy_note(&Err(err)), format!("✗ reverse proxy refused: {refused}"));
    let ok = json!({"changed": {"allowed_origins": {"applies": "live"}, "trust_proxy": {"applies": "overridden_by_env"}}});
    let note = network_proxy_note(&Ok(ok));
    assert!(note.contains("origins saved, applies now"), "{note}");
    assert!(note.contains("trust proxy saved, NOT in effect"), "{note}");
    assert_eq!(network_proxy_note(&Ok(json!({"changed": {}}))), "✓ reverse proxy: no change");
}

fn apps_runtime_config() -> Value {
    json!({
        "writable": true,
        "apps": {
            "host": {"key": "apps.host", "label": "Where apps listen", "help": "127.0.0.1 = this computer only.",
                     "placeholder": "127.0.0.1", "value": "0.0.0.0", "source": "stored", "default": "127.0.0.1"},
            "ports": {"key": "apps.ports", "label": "Ports for apps", "help": "A port or a range.",
                      "placeholder": "3100-3199", "value": "", "source": "default", "default": ""},
            "node": {"key": "apps.node", "label": "Node.js for apps", "help": "auto, managed, system or a path.",
                     "placeholder": "auto", "value": "system", "source": "env", "default": "auto",
                     "note": "from ABSTRACTGATEWAY_APPS_NODE in the environment this gateway was started with; a saved value replaces it"}
        }
    })
}

#[test]
fn apps_settings_render_in_runtime_knobs_with_their_source() {
    let mut h = harness_sized(Size::new(120, 70));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.ui.rt_knobs_folded.set(false);
    h.turns(2);
    h.store.runtime_config.set(Loadable::Ready(
        abstractgateway_console::store::RuntimeConfigData::from_value(&apps_runtime_config()),
    ));
    let s = h.turns(2);
    assert!(s.contains("apps.host: 0.0.0.0  (stored)"), "host row:\n{s}");
    assert!(s.contains("apps.node: system  (env)"), "env-sourced row:\n{s}");
    assert!(s.contains("apps.ports: —  (default)"), "default row:\n{s}");
    assert!(s.contains("Edit apps settings"), "editor entry point:\n{s}");
    if let Ok(dir) = std::env::var("MISSION_Z_RENDER_DIR") {
        std::fs::write(format!("{dir}/tui_runtime_knobs_apps.txt"), &s).expect("write render");
    }
}

#[test]
fn apps_settings_body_sends_only_changed_keys_and_clears_with_empty() {
    use abstractgateway_console::ui::runtimes::apps_settings_body;

    let d = abstractgateway_console::store::RuntimeConfigData::from_value(&apps_runtime_config());
    assert_eq!(d.apps.len(), 3);
    // Unchanged (stored host kept, env/default fields left empty) -> nothing.
    let same = vec![("host".to_string(), "0.0.0.0".to_string()), ("ports".to_string(), String::new()), ("node".to_string(), String::new())];
    assert_eq!(apps_settings_body(&d.apps, &same), json!({}));
    let typed = vec![
        ("host".to_string(), String::new()),              // clear the stored value
        ("ports".to_string(), " 3200-3299 ".to_string()),  // new
        ("node".to_string(), String::new()),              // env value never promoted to stored
    ];
    assert_eq!(apps_settings_body(&d.apps, &typed), json!({"apps.host": "", "apps.ports": "3200-3299"}));
}

fn agents_runtime_config() -> Value {
    json!({
        "writable": true,
        "agents": {
            "label": "Default agent workflow",
            "index_source": "host",
            "default_workflow": {
                "abstractcode.agent.v1": {
                    "key": "agents.default_workflow.abstractcode.agent.v1", "value": "coder:code", "source": "stored",
                    "available": true, "reason": null, "default": "basic-agent:ba",
                    "resolved": {"workflow_id": "coder@1.1.0:code", "name": "Coder", "bundle_id": "coder",
                                 "bundle_version": "1.1.0", "flow_id": "code", "registry_scope": "private"},
                    "eligible": [{"value": "basic-agent:ba"}, {"value": "coder:code"}]
                },
                "abstractassistant.agent.v1": {
                    "key": "agents.default_workflow.abstractassistant.agent.v1", "value": null, "source": "default",
                    "available": false, "resolved": null, "default": null, "eligible": [],
                    "reason": "no host workflow declares abstractassistant.agent.v1; the Assistant uses its built-in orchestrator"
                }
            }
        }
    })
}

#[test]
fn agent_defaults_parse_render_and_body() {
    use abstractgateway_console::ui::runtimes::agent_defaults_body;

    let d = abstractgateway_console::store::RuntimeConfigData::from_value(&agents_runtime_config());
    assert_eq!(d.agent_defaults.len(), 2);
    let code = d.agent_defaults.iter().find(|a| a.interface == "abstractcode.agent.v1").unwrap();
    assert!(code.available && code.workflow_id == "coder@1.1.0:code" && code.source == "stored");
    assert_eq!(code.eligible, vec!["basic-agent:ba".to_string(), "coder:code".to_string()]);
    let assist = d.agent_defaults.iter().find(|a| a.interface == "abstractassistant.agent.v1").unwrap();
    assert!(!assist.available && assist.reason.contains("built-in orchestrator"));

    // Body: only changed rows; "" clears; a default-sourced row left empty sends nothing.
    let same = vec![
        ("abstractcode.agent.v1".to_string(), "coder:code".to_string()),
        ("abstractassistant.agent.v1".to_string(), String::new()),
    ];
    assert_eq!(agent_defaults_body(&d.agent_defaults, &same), json!({}));
    let typed = vec![("abstractcode.agent.v1".to_string(), String::new())];
    assert_eq!(
        agent_defaults_body(&d.agent_defaults, &typed),
        json!({"agents": {"default_workflow": {"abstractcode.agent.v1": ""}}})
    );

    // Render: one knob row per interface with what it runs or why not.
    let mut h = harness_sized(Size::new(140, 70));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.ui.rt_knobs_folded.set(false);
    h.turns(2);
    h.store.runtime_config.set(Loadable::Ready(d));
    let s = h.turns(2);
    assert!(s.contains("abstractcode.agent.v1 → coder@1.1.0:code (Coder)  (stored)"), "code row:\n{s}");
    assert!(s.contains("abstractassistant.agent.v1 → unavailable: no host workflow"), "assistant row:\n{s}");
    assert!(s.contains("Edit default agent workflows"), "editor entry point:\n{s}");
}

#[test]
fn workflows_payload_carries_agent_default_marks() {
    use abstractgateway_console::store::workflows_from_payload;
    use abstractgateway_console::ui::workflows::agent_default_marks;

    let d = workflows_from_payload(&json!({
        "items": [],
        "default_bundle_id": null,
        "default_agent_workflows": {
            "abstractcode.agent.v1": {"workflow_id": "coder@1.1.0:code", "bundle_id": "coder", "source": "stored"}
        }
    }));
    assert_eq!(d.agent_defaults, vec![("abstractcode.agent.v1".to_string(), "coder@1.1.0:code".to_string())]);
    assert_eq!(agent_default_marks("coder", &d.agent_defaults).len(), 1);
    assert!(agent_default_marks("code", &d.agent_defaults).is_empty(), "a prefix of another bundle id must not match");
}


#[test]
fn skills_shelf_parse_render_and_body() {
    use abstractgateway_console::ui::runtimes::skills_shelf_body;

    let v = json!({"writable": true, "skills": {"shelf": {
        "key": "skills.shelf", "value": null, "source": "seeded", "resolved": "/d/skills/registry",
        "available": true, "reason": null, "default_path": "/d/skills/registry", "bundled_version": "2026.09.25"}}});
    let d = abstractgateway_console::store::RuntimeConfigData::from_value(&v);
    let sh = d.skills_shelf.clone().expect("shelf parsed");
    assert!(sh.available && sh.source == "seeded" && sh.bundled_version == "2026.09.25");
    assert_eq!(skills_shelf_body(&sh, ""), json!({}));
    assert_eq!(skills_shelf_body(&sh, " /x/reg "), json!({"skills.shelf": "/x/reg"}));
    let stored = abstractgateway_console::store::SkillsShelf { value: "/x".into(), source: "stored".into(), ..Default::default() };
    assert_eq!(skills_shelf_body(&stored, ""), json!({"skills.shelf": ""}));

    let mut h = harness_sized(Size::new(140, 70));
    h.connect_as_admin();
    h.goto_screen(4);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(2);
    h.ui.rt_knobs_folded.set(false);
    h.turns(2);
    h.store.runtime_config.set(Loadable::Ready(d));
    let s = h.turns(2);
    assert!(s.contains("skills.shelf: /d/skills/registry (curated 2026.09.25)  (seeded)"), "shelf row:\n{s}");
    assert!(s.contains("Edit skills shelf"), "editor entry point:\n{s}");
}

// ---------------------------------------------------------------------
// G2: About modal (F1 / ? / Connection button) and the stream-replies knob.
// ---------------------------------------------------------------------

#[test]
fn about_modal_lists_the_identity_and_the_gateway_versions() {
    let mut h = harness_sized(Size::new(120, 50));
    h.connect_as_admin();
    h.goto_screen(1);
    // F1 = ESC O P (xterm SS3).
    h.key(b"\x1bOP");
    let s = h.turns(2);
    assert!(
        matches!(h.find_cmd(|c| matches!(c, Cmd::LoadAbout)), Some(Cmd::LoadAbout)),
        "opening About (connected) reads GET /about"
    );
    assert!(s.contains("reading GET /api/gateway/about"), "loading row:\n{s}");
    h.store.about.set(Loadable::Ready(json!({
        "abstractgateway": "0.4.4", "abstractframework": "0.3.4",
        "packages": {"abstractcore": "2.15.3", "abstractgateway": "0.4.4", "abstractruntime": "0.4.35"}
    })));
    let s = h.turns(2);
    for needle in [
        &format!("AbstractGateway console {}", env!("CARGO_PKG_VERSION")),
        "Part of AbstractFramework — https://abstractframework.ai",
        "Author: Laurent-Philippe Albou, PhD (2023-2026)",
        "© 2023-2026 Laurent-Philippe Albou, PhD. Released under the MIT License.",
        "Website: https://abstractframework.ai/gateway",
        "Source: https://github.com/lpalbou/AbstractGateway",
        "Documentation: https://www.lpalbou.info/AbstractGateway/",
        "Report an issue: https://github.com/lpalbou/AbstractGateway/issues",
        "Give feedback: https://github.com/lpalbou/AbstractGateway/issues/new?labels=feedback",
        "Contact: contact@abstractframework.ai",
        "Gateway: AbstractGateway 0.4.4",
        "Gateway framework: AbstractFramework 0.3.4",
        "Gateway package abstractcore: 2.15.3",
        "Gateway package abstractruntime: 0.4.35",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    assert!(!s.contains("Gateway package abstractgateway"), "the gateway itself is not a package row");
    h.press_escape();
    let s = h.turns(2);
    assert!(!s.contains("Report an issue:"), "Esc closes About:\n{s}");
}

#[test]
fn about_modal_says_why_the_gateway_rows_are_missing() {
    use abstractgateway_console::api::{ApiError, ApiErrorKind};
    use abstractgateway_console::ui::about::gateway_rows;

    // Not connected: no read is sent, the row says why.
    let mut h = harness_sized(Size::new(120, 50));
    h.goto_screen(0);
    h.key(b"\x1bOP");
    let s = h.turns(2);
    assert!(h.find_cmd(|c| matches!(c, Cmd::LoadAbout)).is_none(), "no read while not connected");
    assert!(s.contains("Gateway: unavailable (not connected to a gateway"), "not-connected row:\n{s}");
    assert!(s.contains("AbstractGateway console"), "identity still shown:\n{s}");
    // A failed read is one visible row.
    let failed: Loadable<Value> = Loadable::Failed(ApiError::new(ApiErrorKind::Unreachable, "HTTP 404"));
    let rows = gateway_rows(true, &failed);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].0, "Gateway");
    assert!(rows[0].1.starts_with("unavailable (") && rows[0].1.contains("HTTP 404"), "{rows:?}");
}

#[test]
fn about_is_on_the_connection_screen_and_question_mark() {
    let mut h = harness_sized(Size::new(120, 50));
    h.goto_screen(0);
    let s = h.turns(2);
    assert!(s.contains("About: F1 (or ?)"), "Connection screen names the About key:\n{s}");
    h.connect_as_admin();
    h.goto_screen(3);
    h.key(b"?");
    let s = h.turns(2);
    assert!(s.contains("Contact: contact@abstractframework.ai"), "? opens About:\n{s}");
}

#[test]
fn about_flag_text_carries_every_line() {
    let ok = abstractgateway_console::about_text(Ok(json!({"abstractgateway": "0.4.4"})));
    assert!(ok.starts_with(&format!("AbstractGateway console {}\n", env!("CARGO_PKG_VERSION"))), "{ok}");
    assert!(ok.contains("Gateway: AbstractGateway 0.4.4") && ok.contains("Gateway framework: not installed on the gateway host"));
    let down = abstractgateway_console::about_text(Err("network failure: refused".into()));
    assert!(down.ends_with("Gateway: unavailable (network failure: refused)"), "{down}");
    assert!(down.contains("Contact: contact@abstractframework.ai"));
}

#[test]
fn streaming_default_knob_reads_edits_and_never_hides() {
    use abstractgateway_console::store::streaming_default_from;
    use abstractgateway_console::ui::runtimes::streaming_default_body;

    let on = json!({"writable": true, "agents": {"streaming_default": {
        "key": "agents.streaming_default", "value": true, "source": "stored", "default": false,
        "label": "Stream replies by default", "help": "Interactive runs only."}}});
    let sd = streaming_default_from(&on).expect("parsed");
    assert!(sd.value && sd.source == "stored");
    assert_eq!(streaming_default_body(&sd, true), json!({}));
    assert_eq!(streaming_default_body(&sd, false), json!({"agents": {"streaming_default": false}}));
    assert!(streaming_default_from(&json!({"agents": {"default_workflow": {}}})).is_none());
    assert!(streaming_default_from(&json!({"agents": {"streaming_default": {"value": "yes", "source": "stored"}}})).is_none());

    for (payload, want, button) in [
        (on.clone(), "stream replies: on — interactive replies stream live  (stored)", true),
        (
            json!({"writable": true, "agents": {"streaming_default": {"value": false, "source": "default"}}}),
            "stream replies: off — replies arrive whole  (default)",
            true,
        ),
        (
            json!({"writable": true, "agents": {"default_workflow": {}}}),
            "stream replies: not available on this gateway",
            false,
        ),
    ] {
        // A real read always carries the workspace knobs beside it.
        let mut payload = payload;
        payload["workspace_root"] = json!({"value": "/w", "source": "stored"});
        let d = abstractgateway_console::store::RuntimeConfigData::from_value(&payload);
        let mut h = harness_sized(Size::new(160, 70));
        h.connect_as_admin();
        h.goto_screen(4);
        h.store
            .runtimes
            .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
        h.turns(2);
        h.ui.rt_knobs_folded.set(false);
        h.turns(2);
        h.store.runtime_config.set(Loadable::Ready(d));
        let s = h.turns(2);
        assert!(s.contains(want), "row {want:?}:\n{s}");
        assert_eq!(s.contains("Edit stream replies"), button, "edit entry point:\n{s}");
    }
}
