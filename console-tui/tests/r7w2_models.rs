//! The Models page (round 7, R7.2: the web console's consolidated catalog
//! in the terminal) through the REAL interface, headless: the page reads
//! and writes ONLY through the plain JSON lane, so every action is pinned
//! by the exact route + body it sends, and every screen state by its text.
//!
//! Fixtures are trimmed live payloads (tests/fixtures/r7w2_models_*.json):
//! four catalog models, the engines' installed list (two of its rows are in
//! no catalog entry), and the capability defaults.
//!
//! `R7W2_SHOTS=<dir>` writes each named screen as text.

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
use abstractgateway_console::ui::{self, catalog, Ctx, UiState};
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
        panic!("the Models page never uses the shared screens' transport")
    }
    fn models_installed(&self, _p: Option<&str>) -> Result<Value, TransportError> {
        panic!("the Models page never uses the shared screens' transport")
    }
    fn start_download(&self, _p: &str, _a: &str, _b: Option<u64>) -> Result<Value, TransportError> {
        panic!("the Models page never uses the shared screens' transport")
    }
    fn delete_model(&self, _p: &str, _a: &str, _f: bool) -> Result<Value, TransportError> {
        panic!("the Models page never uses the shared screens' transport")
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

struct H {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
    engine_filter: Signal<Option<String>>,
}

fn fixture(name: &str) -> Value {
    let path = format!(
        "{}/tests/fixtures/r7w2_models_{name}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json")
}

fn harness(size: Size) -> H {
    catalog::reset_state();
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let slot: Rc<RefCell<Option<(Store, UiState)>>> = Rc::new(RefCell::new(None));
    let out = slot.clone();
    let ef_slot: Rc<RefCell<Option<Signal<Option<String>>>>> = Rc::new(RefCell::new(None));
    let ef_out = ef_slot.clone();
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
        *ef_out.borrow_mut() = Some(screens.store.engine_filter);
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
    let engine_filter = ef_slot.borrow().expect("engine filter");
    H {
        app,
        term,
        driver,
        store,
        ui,
        rx,
        engine_filter,
    }
}

fn identity(admin: bool) -> Identity {
    Identity::from_me(&json!({
        "ok": true,
        "principal": if admin {
            json!({"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "admin": true})
        } else {
            json!({"user_id": "alice", "tenant_id": "default", "roles": ["user"], "admin": false})
        },
        "auth": {"mode": "users"},
        "routing": {"mode": "per-principal"}
    }))
    .unwrap()
}

/// A read the page sent (key, path).
#[derive(Debug, Clone, PartialEq)]
struct Sent {
    key: String,
    method: String,
    path: String,
    body: Value,
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
    fn esc(&mut self) -> String {
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(std::time::Duration::from_millis(45));
        self.turns(3)
    }
    /// Every JSON-lane command sent since the last call.
    fn sent(&mut self) -> Vec<Sent> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            match c {
                Cmd::Json(JsonCmd::Get { key, path, .. }) => out.push(Sent {
                    key,
                    method: "GET".into(),
                    path,
                    body: Value::Null,
                }),
                Cmd::Json(JsonCmd::Send {
                    key,
                    method,
                    path,
                    body,
                    ..
                }) => out.push(Sent {
                    key,
                    method,
                    path,
                    body,
                }),
                _ => {}
            }
        }
        out
    }
    /// Connected, on the Models page, the four reads answered.
    fn open(&mut self, admin: bool) -> String {
        self.store.conn.set(ConnPhase::Connected(identity(admin)));
        self.turns(1);
        self.ui.wizard.set(false);
        self.ui.screen.set(ui::SCREEN_CATALOG);
        self.turns(3);
        let sent = self.sent();
        let gets: Vec<&str> = sent
            .iter()
            .filter(|s| s.method == "GET")
            .map(|s| s.path.as_str())
            .collect();
        for p in [
            "/models/catalog",
            "/models/installed",
            "/config/capability-defaults",
            "/models/downloads",
        ] {
            assert_eq!(
                gets.iter().filter(|g| **g == p).count(),
                1,
                "{p} read once on entry: {gets:?}"
            );
        }
        let j = self.store.json;
        j.set(catalog::K_CATALOG, Loadable::Ready(fixture("catalog")));
        j.set(catalog::K_INSTALLED, Loadable::Ready(fixture("installed")));
        j.set(catalog::K_DEFAULTS, Loadable::Ready(fixture("defaults")));
        j.set(
            catalog::K_DOWNLOADS,
            Loadable::Ready(json!({"ok": true, "jobs": []})),
        );
        self.turns(3)
    }
    /// Move the selection to the artifact row `k` (provider/artifact).
    fn select(&mut self, k: &str) -> String {
        for _ in 0..40 {
            if catalog::with_state(|p| p.sel.clone()).as_deref() == Some(k) {
                return self.turns(1);
            }
            self.key(b"\x1b[B");
        }
        panic!(
            "row {k} never selected: at {:?}",
            catalog::with_state(|p| p.sel.clone())
        );
    }
    fn answer(&mut self, key: &str, w: WriteState) -> String {
        self.store.json.set_write(key, Some(w));
        self.turns(3)
    }
    fn shoot(&mut self, name: &str) {
        let s = self.turns(1);
        if let Ok(dir) = std::env::var("R7W2_SHOTS") {
            let size = self.term.screen().size();
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(format!("{dir}/models-{name}-{}x{}.txt", size.w, size.h), s).unwrap();
        }
    }
}

fn refused(status: u16, body: Value) -> ApiError {
    let mut e = ApiError::new(ApiErrorKind::Http(status), format!("HTTP {status}"));
    e.body = Some(body);
    e
}

// ---------------------------------------------------------------------------

#[test]
fn one_list_with_model_headers_artifact_rows_and_not_in_the_catalog() {
    let mut h = harness(Size::new(170, 50));
    let s = h.open(true);
    assert!(s.contains("9 Models"), "tab 9 is Models:\n{s}");
    for needle in [
        "This computer: Apple M5 Max",
        "4 of 4 models · 16 artifacts shown",
        "Qwen3 0.6B  Qwen · 600M params · apache-2.0  [Text] [Thinking] [Tools]",
        "qwen3:0.6b-q8_0",
        "Downloaded · Fits",
        "Not downloaded · Fits",
        "Not downloaded · Too large",
        "u Use as default · d Delete",
        "w Download",
        "Not in the catalog",
        "my-finetune:7b",
        "mlx-community/my-own-tune-4bit",
        "z Quantization: [All] 4-bit",
        "s Status: [All] Downloaded 4 Not downloaded",
        "It fits once macOS lets the GPU use 96 GiB: run sudo sysctl iogpu.wired_limit_mb=98304",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    // The recommended build carries the accent dot, only beside its siblings.
    assert!(s.contains("● MLX"), "{s}");
    // An embedding model can never be the default text model.
    let emb_line = s
        .lines()
        .find(|l| l.contains("nomic-embed-text "))
        .unwrap_or_default();
    assert!(!emb_line.contains("Use as default"), "{emb_line}");
    h.shoot("all");
}

#[test]
fn downloaded_filter_keeps_the_rows_outside_the_catalog() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    let s = h.key(b"s");
    assert!(
        s.contains("1 of 4 models · 4 artifacts shown  · Downloaded"),
        "{s}"
    );
    assert!(
        !s.contains("qwen3:0.6b-q8_0"),
        "not downloaded rows hidden:\n{s}"
    );
    assert!(
        s.contains("Not in the catalog") && s.contains("my-finetune:7b"),
        "{s}"
    );
    h.shoot("downloaded");
    // Not downloaded: the extras go (they are downloaded by definition).
    let s = h.key(b"s");
    assert!(s.contains("[Not downloaded"), "{s}");
    assert!(!s.contains("Not in the catalog"), "{s}");
    // Back to All.
    let s = h.key(b"s");
    assert!(s.contains("s Status: [All]"), "{s}");
}

#[test]
fn filters_quant_provider_capability_fits_search_and_clear() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    let s = h.key(b"z");
    assert!(s.contains("[4-bit"), "{s}");
    assert!(!s.contains("qwen3:0.6b-q8_0"), "8-bit hidden:\n{s}");
    let s = h.key(b"z");
    assert!(s.contains("[8-bit") && s.contains("qwen3:0.6b-q8_0"), "{s}");
    h.key(b"x");
    // Capability: All → Text → … Embedding.
    let mut s = String::new();
    for _ in 0..8 {
        s = h.key(b"t");
        if s.contains("[Embedding") {
            break;
        }
    }
    assert!(
        s.contains("[Embedding") && s.contains("nomic-embed-text"),
        "{s}"
    );
    assert!(!s.contains("Qwen3 0.6B"), "{s}");
    h.key(b"x");
    // Fits this computer hides the too-large builds and the outside rows.
    let s = h.key(b"f");
    assert!(s.contains("[x] Fits this computer"), "{s}");
    assert!(!s.contains("minimax"), "{s}");
    assert!(!s.contains("Not in the catalog"), "{s}");
    h.key(b"f");
    // Provider steps through the engines (catalog order).
    let s = h.key(b"p");
    assert!(s.contains("p Provider: All [Ollama"), "{s}");
    assert!(!s.contains("mlx-community/Qwen3-0.6B-4bit "), "{s}");
    h.key(b"x");
    // Search: `/` takes the keyboard, the words filter as typed, Esc gives
    // the keys back.
    h.key(b"/");
    h.term.push_input(b"nomic");
    let s = h.turns(3);
    assert!(s.contains("1 of 4 models") && s.contains("“nomic”"), "{s}");
    h.esc();
    let s = h.key(b"x");
    assert!(s.contains("4 of 4 models"), "x clears:\n{s}");
    // No read was sent for any of it (filters hide, client side).
    assert!(
        h.sent()
            .iter()
            .all(|c| c.method != "GET" || c.key.starts_with("catalog.job")),
        "filters never re-read"
    );
}

#[test]
fn delete_asks_a_dry_run_confirms_inline_and_deletes() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("ollama/qwen3:0.6b");
    h.sent();
    h.key(b"d");
    let sent = h.sent();
    let plan = sent
        .iter()
        .find(|c| c.key == "catalog.delplan:ollama/qwen3:0.6b")
        .expect("dry run sent");
    assert_eq!(plan.method, "POST");
    assert_eq!(plan.path, "/models/delete-download");
    assert_eq!(
        plan.body,
        json!({"provider": "ollama", "artifact": "qwen3:0.6b", "dry_run": true})
    );
    let s = h.turns(1);
    assert!(s.contains("Checking..."), "{s}");
    let s = h.answer(
        "catalog.delplan:ollama/qwen3:0.6b",
        WriteState::Done(
            json!({"ok": true, "status": "planned", "freed_bytes": 522653767, "also_used_by": []}),
        ),
    );
    assert!(
        s.contains(
            "Deletes 523 MB from this computer. Files only — nothing in your runs is touched."
        ),
        "{s}"
    );
    assert!(s.contains("[y] Delete  [n] Keep"), "{s}");
    h.shoot("delete-confirm");
    // n keeps: nothing sent, the trash verb is back.
    let s = h.key(b"n");
    assert!(!s.contains("[y] Delete"), "{s}");
    assert!(h.sent().is_empty(), "Keep sends nothing");
    assert!(s.contains("d Delete"), "{s}");
    // Again, and y deletes.
    h.key(b"d");
    h.sent();
    h.answer(
        "catalog.delplan:ollama/qwen3:0.6b",
        WriteState::Done(json!({"ok": true, "freed_bytes": 522653767})),
    );
    h.key(b"y");
    let del = h
        .sent()
        .into_iter()
        .find(|c| c.key == "catalog.delete:ollama/qwen3:0.6b")
        .expect("delete sent");
    assert_eq!(
        del.body,
        json!({"provider": "ollama", "artifact": "qwen3:0.6b", "dry_run": false})
    );
    assert_eq!(del.path, "/models/delete-download");
    let s = h.answer(
        "catalog.delete:ollama/qwen3:0.6b",
        WriteState::Done(json!({"ok": true, "status": "deleted", "freed_bytes": 522653767})),
    );
    assert!(s.contains("Download deleted. 523 MB freed."), "{s}");
    // The list is read again (the engines' answer decides).
    let reads: Vec<String> = h
        .sent()
        .into_iter()
        .filter(|c| c.method == "GET")
        .map(|c| c.path)
        .collect();
    assert!(reads.contains(&"/models/catalog".to_string()), "{reads:?}");
}

#[test]
fn a_refused_delete_says_the_gateways_words() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("mlx/mlx-community/Qwen3-0.6B-4bit");
    h.key(b"d");
    let s = h.answer(
        "catalog.delplan:mlx/mlx-community/Qwen3-0.6B-4bit",
        WriteState::Failed(refused(
            409,
            json!({"ok": false, "status": "refused", "reason": "resident",
                   "message": "This model is loaded right now.", "fix": "Unload it first."}),
        )),
    );
    assert!(
        s.contains("This model is loaded right now. Unload it first."),
        "{s}"
    );
    assert!(
        !s.contains("[y] Delete"),
        "no confirmation for a refusal:\n{s}"
    );
    h.shoot("delete-refused");
}

#[test]
fn a_row_outside_the_catalog_deletes_with_its_own_notice() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("ollama/my-finetune:7b");
    h.key(b"d");
    h.answer(
        "catalog.delplan:ollama/my-finetune:7b",
        WriteState::Done(json!({"ok": true, "freed_bytes": 4683087332u64})),
    );
    let s = h.turns(1);
    assert!(s.contains("Deletes 4.7 GB from this computer."), "{s}");
    h.key(b"y");
    let s = h.answer(
        "catalog.delete:ollama/my-finetune:7b",
        WriteState::Done(json!({"ok": true, "freed_bytes": 4683087332u64})),
    );
    assert!(s.contains("Deleted my-finetune:7b. 4.7 GB freed."), "{s}");
    assert!(
        !s.contains("my-finetune:7b  "),
        "the row is gone until the re-read:\n{s}"
    );
}

#[test]
fn use_as_default_puts_exactly_the_chosen_model() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("mlx/mlx-community/Qwen3-0.6B-4bit");
    h.sent();
    h.key(b"u");
    let put = h
        .sent()
        .into_iter()
        .find(|c| c.key == "catalog.default")
        .expect("default sent");
    assert_eq!(put.method, "PUT");
    assert_eq!(put.path, "/config/capability-defaults/output/text");
    assert_eq!(
        put.body,
        json!({"provider": "mlx", "model": "mlx-community/Qwen3-0.6B-4bit",
               "base_url": "", "reasoning": "", "options": {}})
    );
    // The re-read shows the new route; the page says so and marks the row.
    let mut d = fixture("defaults");
    for r in d["routes"].as_array_mut().unwrap() {
        if r["key"] == "output.text" {
            r["provider"] = json!("mlx");
            r["model"] = json!("mlx-community/Qwen3-0.6B-4bit");
        }
    }
    h.store.json.set(catalog::K_DEFAULTS, Loadable::Ready(d));
    let s = h.answer("catalog.default", WriteState::Done(json!({"ok": true})));
    assert!(
        s.contains("Default text model: MLX · mlx-community/Qwen3-0.6B-4bit."),
        "{s}"
    );
    assert!(s.contains("Default text model · d Delete"), "{s}");
    h.shoot("use-as-default");
}

#[test]
fn download_follows_the_job_and_cancel_is_two_steps() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("ollama/qwen3:0.6b-q8_0");
    h.sent();
    h.key(b"w");
    let post = h
        .sent()
        .into_iter()
        .find(|c| c.key == "catalog.download:ollama/qwen3:0.6b-q8_0")
        .expect("download sent");
    assert_eq!(post.path, "/models/download");
    assert_eq!(
        post.body,
        json!({"provider": "ollama", "artifact": "qwen3:0.6b-q8_0", "expected_bytes": 832291421u64}),
        "expected_bytes rides along for a catalog-sized build (the gateway's disk pre-check)"
    );
    let s = h.turns(1);
    assert!(s.contains("Starting..."), "{s}");
    let job = json!({"job": "dl_1", "provider": "ollama", "artifact": "qwen3:0.6b-q8_0",
                     "status": "running", "state": "downloading", "percent": 42.0,
                     "bytes_done": 349000000u64, "bytes_total": 832000000u64});
    let s = h.answer(
        "catalog.download:ollama/qwen3:0.6b-q8_0",
        WriteState::Done(json!({"ok": true, "job": job})),
    );
    assert!(s.contains("Downloading · 42% · 349 MB of 832 MB"), "{s}");
    assert!(s.contains("c Cancel"), "{s}");
    let polls: Vec<String> = h.sent().into_iter().map(|c| c.path).collect();
    assert!(
        polls.contains(&"/models/download/dl_1".to_string()),
        "{polls:?}"
    );
    // Cancel: c only asks; y stops it.
    let s = h.key(b"c");
    assert!(
        s.contains("Stop this download?") && s.contains("[y] Stop download"),
        "{s}"
    );
    assert!(h
        .sent()
        .iter()
        .all(|c| !c.key.starts_with("catalog.cancel")));
    h.key(b"y");
    let cancel = h
        .sent()
        .into_iter()
        .find(|c| c.key == "catalog.cancel:dl_1")
        .expect("cancel sent");
    assert_eq!(cancel.path, "/models/download/dl_1/cancel");
    assert_eq!(cancel.body, json!({"via": "console"}));
    let s = h.turns(1);
    assert!(s.contains("Cancelling"), "{s}");
    // The job ends: the page reads the catalog again.
    h.store.json.set(
        "catalog.job:dl_1",
        Loadable::Ready(
            json!({"ok": true, "job": {"job": "dl_1", "provider": "ollama",
            "artifact": "qwen3:0.6b-q8_0", "status": "cancelled"}}),
        ),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Download cancelled. Download it again any time."),
        "{s}"
    );
    let reads: Vec<String> = h.sent().into_iter().map(|c| c.path).collect();
    assert!(reads.contains(&"/models/catalog".to_string()), "{reads:?}");
}

#[test]
fn a_download_that_fails_to_start_says_why() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("ollama/qwen3:0.6b-q8_0");
    h.key(b"w");
    let s = h.answer(
        "catalog.download:ollama/qwen3:0.6b-q8_0",
        WriteState::Failed(refused(
            409,
            json!({"detail": "Not enough free disk space."}),
        )),
    );
    assert!(
        s.contains("Could not start the download: Not enough free disk space."),
        "{s}"
    );
}

#[test]
fn a_non_admin_sees_the_list_but_no_verbs() {
    let mut h = harness(Size::new(170, 50));
    let s = h.open(false);
    assert!(s.contains("Download (admin only)"), "{s}");
    assert!(!s.contains("w Download"), "{s}");
    h.select("ollama/qwen3:0.6b-q8_0");
    h.key(b"w");
    assert!(
        h.sent().iter().all(|c| c.method == "GET"),
        "nothing written by a non-admin"
    );
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Only an admin can download models")
    );
}

#[test]
fn hugging_face_mode_asks_the_hub_once_per_enter() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    let s = h.key(b"m");
    assert!(s.contains("m Catalog [Hugging Face]"), "{s}");
    assert!(
        s.contains("Search Hugging Face") && s.contains("Type a model name above and press Enter."),
        "{s}"
    );
    h.key(b"/");
    h.term.push_input(b"qwen tiny");
    h.turns(2);
    assert!(h.sent().is_empty(), "typing does not search the Hub");
    h.key(b"\r");
    let hub = h
        .sent()
        .into_iter()
        .find(|c| c.key == catalog::K_HUB)
        .expect("hub search");
    assert_eq!(hub.path, "/models/catalog?q=qwen%20tiny&hub=true");
    let s = h.turns(1);
    assert!(
        s.contains("Searching Hugging Face for “qwen tiny”..."),
        "{s}"
    );
    h.store.json.set(
        catalog::K_HUB,
        Loadable::Ready(json!({"schema": "model_catalog_v1", "rows": [], "hub": {"ok": true}})),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Hugging Face has no model matching “qwen tiny”."),
        "{s}"
    );
    assert!(
        !s.contains("Not in the catalog"),
        "never in Hugging Face mode:\n{s}"
    );
}

#[test]
fn the_catalog_error_and_loading_sentences_are_the_webs() {
    let mut h = harness(Size::new(170, 50));
    h.store.conn.set(ConnPhase::Connected(identity(true)));
    h.turns(1);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_CATALOG);
    let s = h.turns(3);
    assert!(s.contains("Loading the model catalog..."), "{s}");
    h.store.json.set(
        catalog::K_CATALOG,
        Loadable::Failed(ApiError::new(
            ApiErrorKind::Unreachable,
            "connection refused",
        )),
    );
    h.store.json.set(
        catalog::K_INSTALLED,
        Loadable::Failed(ApiError::new(ApiErrorKind::Unreachable, "engines down")),
    );
    let s = h.turns(3);
    assert!(
        s.contains("The model catalog did not load.") && s.contains("connection refused"),
        "{s}"
    );
}

#[test]
fn enter_expands_a_row_with_its_facts() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.select("ollama/qwen3:0.6b");
    let s = h.key(b"\r");
    assert!(s.contains("Artifact: qwen3:0.6b"), "{s}");
    assert!(
        s.contains("Needs about") && s.contains("this computer can give a model"),
        "{s}"
    );
    assert!(
        s.contains("CLI: abstractcore models download ollama qwen3:0.6b"),
        "{s}"
    );
    let s = h.key(b"\r");
    assert!(
        !s.contains("Artifact: qwen3:0.6b"),
        "Enter folds it again:\n{s}"
    );
}

#[test]
fn usable_at_80x24_and_never_wider_than_the_screen() {
    let mut h = harness(Size::new(80, 24));
    let s = h.open(true);
    assert!(s.contains("Qwen3 0.6B"), "{s}");
    assert!(
        s.contains("z Quantization: [All]"),
        "narrow chips line:\n{s}"
    );
    for l in s.lines() {
        assert!(abstracttui::text::width(l) <= 80, "overflow: {l:?}");
    }
    h.shoot("all");
    let s = h.key(b"s");
    assert!(s.contains("Not in the catalog"), "{s}");
    h.shoot("downloaded");
    // The page message shows at this width too.
    h.select("mlx/mlx-community/Qwen3-0.6B-4bit");
    h.key(b"u");
    let s = h.answer(
        "catalog.default",
        WriteState::Failed(ApiError::new(ApiErrorKind::Http(403), "admin only")),
    );
    assert!(s.contains("Could not set the default: admin only"), "{s}");
}

#[test]
fn a_reconnect_reads_the_page_again() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    h.key(b"s");
    h.store.conn.set(ConnPhase::Probing);
    h.store.reset_domains();
    h.turns(3);
    assert!(
        h.sent().iter().all(|c| c.method != "GET"),
        "no read while not connected"
    );
    h.store.conn.set(ConnPhase::Connected(identity(true)));
    h.turns(3);
    let reads: Vec<String> = h.sent().into_iter().map(|c| c.path).collect();
    assert!(reads.contains(&"/models/catalog".to_string()), "{reads:?}");
    // The filters are kept; the old gateway's delete state is not.
    assert_eq!(
        catalog::with_state(|p| p.filters.status.clone()),
        "downloaded"
    );
}

// ---- pure helpers ---------------------------------------------------------

#[test]
fn helpers_follow_the_web_page() {
    assert_eq!(
        catalog::served_model_id("lmstudio", "qwen/qwen3-4b@q4_k_m"),
        "qwen/qwen3-4b"
    );
    assert_eq!(catalog::served_model_id("mlx", "a/b@c"), "a/b@c");
    assert_eq!(catalog::ui_bytes(522653767.0), "523 MB");
    assert_eq!(catalog::ui_bytes(4683087332.0), "4.7 GB");
    assert_eq!(catalog::params(Some(600e6)), "600M");
    assert_eq!(catalog::params(Some(1.7e9)), "1.7B");
    assert_eq!(catalog::params(Some(30e9)), "30B");
    assert_eq!(
        catalog::confirm_sentence(&json!({"freed_bytes": 351000000, "also_used_by": ["LM Studio"]})),
        "Deletes 351 MB from this computer. Files only — nothing in your runs is touched. LM Studio uses the same files."
    );
    assert_eq!(
        catalog::confirm_sentence(&json!({})),
        "Deletes this model's files from this computer. Files only — nothing in your runs is touched."
    );
    // A GGUF quant names its repo's files; an Ollama tag defaults to latest.
    assert!(catalog::same_files(
        &json!({"provider": "huggingface", "artifact": "org/repo:Q4_K_M"}),
        &json!({"provider": "huggingface", "artifact": "org/repo"})
    ));
    assert!(catalog::same_files(
        &json!({"provider": "ollama", "artifact": "llama3"}),
        &json!({"provider": "ollama", "artifact": "llama3:latest"})
    ));
    assert!(!catalog::same_files(
        &json!({"provider": "ollama", "artifact": "llama3"}),
        &json!({"provider": "mlx", "artifact": "llama3"})
    ));
    // Column widths: everything fits the width; the id gets what is left.
    let rows = vec![vec![
        "● Hugging Face".to_string(),
        "unsloth/Qwen3-0.6B-GGUF:Q4_K_M".to_string(),
        "4-bit".to_string(),
        "about 375 MB".to_string(),
        "Not downloaded · Fits".to_string(),
        "u Use as default · d Delete".to_string(),
    ]];
    for w in [76, 116, 200] {
        let ws = catalog::art_widths(&rows, w);
        assert!(ws.iter().sum::<i32>() + 2 + 10 <= w.max(42), "{w}: {ws:?}");
    }
}

#[test]
fn browse_models_from_providers_opens_the_engines_builds() {
    let mut h = harness(Size::new(170, 50));
    h.open(true);
    // Providers' Browse models sets the shared engine filter, then jumps.
    h.engine_filter.set(Some("ollama".into()));
    let s = h.turns(3);
    assert!(
        s.contains("p Provider: All [Ollama") || s.contains("[Ollama"),
        "{s}"
    );
    assert!(
        !s.contains("mlx-community/Qwen3-0.6B-4bit "),
        "only Ollama builds:\n{s}"
    );
    assert_eq!(
        h.engine_filter.get_untracked(),
        None,
        "the hand-over is consumed"
    );
}
