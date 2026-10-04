//! The Models page through the REAL worker against a LIVE hermetic gateway
//! (round 7, R7.2): delete with its confirmation, a refused delete, Use as
//! default, the filters — each checked against the gateway's own state with
//! a direct HTTP read. Ignored by default (the scratch gateway of
//! untracked/round4/r7/w2/run_scratch_gateway_b.sh: fake HF artifacts, a
//! fake Ollama whose `qwen3:0.6b` is resident, no provider keys):
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18791 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_r7w2_models -- --ignored --test-threads 1
//!
//! The delete test removes `my-finetune:7b` from the fake Ollama (restart
//! the scratch gateway to get it back).

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{ConnPhase, Store};
use abstractgateway_console::ui::{self, catalog, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

struct NoTransport;

impl ConsoleTransport for NoTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn engines_status(&self, _probe: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn models_catalog(&self, _q: &str, _e: Option<&str>, _f: bool) -> Result<Value, TransportError> {
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
    tx: mpsc::Sender<Cmd>,
    rx: Option<mpsc::Receiver<Cmd>>,
    url: String,
    token: String,
}

fn harness(size: Size) -> H {
    catalog::reset_state();
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
    H {
        app,
        term,
        driver,
        store,
        ui,
        tx: tx_keep,
        rx: Some(rx),
        url: String::new(),
        token: String::new(),
    }
}

impl H {
    fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            self.driver.turn(&mut self.app, &mut self.term).expect("turn");
            last = self.term.screen().to_text();
        }
        last
    }
    fn key(&mut self, bytes: &[u8]) -> String {
        self.term.push_input(bytes);
        self.turns(3)
    }
    fn until(&mut self, what: &str, mut pred: impl FnMut(&mut H, &str) -> bool) -> String {
        let deadline = Instant::now() + Duration::from_secs(30);
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
    fn select(&mut self, k: &str) {
        for _ in 0..400 {
            if catalog::with_state(|p| p.sel.clone()).as_deref() == Some(k) {
                self.turns(1);
                return;
            }
            self.key(b"\x1b[B");
        }
        panic!("row {k} never selected");
    }
    /// A direct read of the gateway (the verification, out of band).
    fn http_get(&self, path: &str) -> Value {
        let resp = ureq::get(&format!("{}/api/gateway{path}", self.url))
            .set("Authorization", &format!("Bearer {}", self.token))
            .call()
            .expect("GET");
        serde_json::from_str(&resp.into_string().unwrap()).unwrap()
    }
    fn shoot(&mut self, name: &str) {
        let s = self.turns(1);
        if let Ok(dir) = std::env::var("R7W2_SHOTS") {
            let size = self.term.screen().size();
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(format!("{dir}/models-live-{name}-{}x{}.txt", size.w, size.h), s).unwrap();
        }
    }
}

fn live(size: Size) -> H {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN");
    let mut h = harness(size);
    h.url = url.clone();
    h.token = token.clone();
    let wake = abstracttui::reactive::wake_handle();
    let ui_state = h.ui;
    let done_sink = {
        let wake = wake.clone();
        move |fid: u64, out: Result<String, String>| {
            wake.post(move || ui_state.write_done.set(Some((fid, out.clone()))))
        }
    };
    let rx = h.rx.take().expect("rx");
    let _worker = abstractgateway_console::worker::spawn(
        h.store,
        wake,
        rx,
        h.tx.clone(),
        |_u: String, _t: String| {},
        done_sink,
    );
    h.ui.wizard.set(false);
    h.ui.conn_url.set(url.clone());
    h.ui.conn_token.set(token.clone());
    h.tx.send(Cmd::Connect {
        url,
        token: token.into(),
    })
    .unwrap();
    h.until("connected", |h, _| {
        h.store.conn.with_untracked(ConnPhase::is_connected)
    });
    h.ui.screen.set(ui::SCREEN_CATALOG);
    h.until("the catalog", |_, s| s.contains("Qwen3 0.6B") && s.contains("artifacts shown"));
    h
}

fn installed_has(v: &Value, provider: &str, artifact: &str) -> bool {
    v["rows"]
        .as_array()
        .unwrap()
        .iter()
        .any(|r| r["provider"] == provider && r["artifact"] == artifact)
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn live_downloaded_filter_lists_the_rows_outside_the_catalog() {
    let mut h = live(Size::new(150, 45));
    let s = h.key(b"s");
    assert!(s.contains("· Downloaded"), "{s}");
    assert!(s.contains("Not in the catalog"), "{s}");
    assert!(s.contains("mlx-community/my-own-tune-4bit"), "{s}");
    h.shoot("downloaded");
    // The same rows the gateway lists (no catalog id).
    let inst = h.http_get("/models/installed");
    for r in inst["rows"].as_array().unwrap() {
        if r["catalog_id"].is_null() {
            assert!(s.contains(r["artifact"].as_str().unwrap()), "{r}:\n{s}");
        }
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn live_a_resident_model_refuses_the_delete_with_the_gateways_words() {
    let mut h = live(Size::new(150, 45));
    h.select("ollama/qwen3:0.6b");
    h.key(b"d");
    let s = h.until("the refusal", |_, s| s.contains("so its files cannot be deleted"));
    assert!(s.contains("Unload it first"), "{s}");
    assert!(!s.contains("[y] Delete"), "{s}");
    h.shoot("delete-refused");
    assert!(installed_has(&h.http_get("/models/installed"), "ollama", "qwen3:0.6b"));
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn live_delete_confirms_with_the_dry_run_size_and_removes_the_files() {
    let mut h = live(Size::new(150, 45));
    assert!(installed_has(&h.http_get("/models/installed"), "ollama", "my-finetune:7b"), "fixture present (restart the scratch gateway)");
    h.select("ollama/my-finetune:7b");
    h.key(b"d");
    let s = h.until("the confirmation", |_, s| s.contains("[y] Delete  [n] Keep"));
    assert!(
        s.contains("Deletes 4.7 GB from this computer. Files only — nothing in your runs is touched."),
        "{s}"
    );
    h.shoot("delete-confirm");
    h.key(b"y");
    let s = h.until("deleted", |_, s| s.contains("Deleted my-finetune:7b. 4.7 GB freed."));
    h.shoot("deleted");
    let _ = s;
    assert!(!installed_has(&h.http_get("/models/installed"), "ollama", "my-finetune:7b"), "gone from the gateway");
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn live_use_as_default_sets_the_text_route() {
    let mut h = live(Size::new(150, 45));
    let before = h.http_get("/config/capability-defaults");
    h.select("mlx/mlx-community/Qwen3-0.6B-4bit");
    h.key(b"u");
    let s = h.until("the default message", |_, s| {
        s.contains("Default text model: MLX · mlx-community/Qwen3-0.6B-4bit.")
    });
    assert!(s.contains("Default text model · d Delete"), "{s}");
    h.shoot("use-as-default");
    let after = h.http_get("/config/capability-defaults");
    let text = after["routes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["key"] == "output.text")
        .cloned()
        .unwrap();
    assert_eq!(text["provider"], "mlx");
    assert_eq!(text["model"], "mlx-community/Qwen3-0.6B-4bit");
    // Put the previous route back (the scratch state stays as it was).
    let prev = before["routes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["key"] == "output.text")
        .cloned()
        .unwrap();
    if prev["read_only"] != json!(true) && prev["provider"].is_string() {
        let _ = ureq::put(&format!("{}/api/gateway/config/capability-defaults/output/text", h.url))
            .set("Authorization", &format!("Bearer {}", h.token))
            .set("Content-Type", "application/json")
            .send_string(&json!({"provider": prev["provider"], "model": prev["model"]}).to_string());
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn live_filters_hide_and_count() {
    let mut h = live(Size::new(150, 45));
    let s = h.key(b"f");
    assert!(s.contains("[x] Fits this computer"), "{s}");
    let mut s = String::new();
    for _ in 0..12 {
        s = h.key(b"t");
        if s.contains("[Embedding") {
            break;
        }
    }
    assert!(s.contains("[Embedding"), "{s}");
    assert!(!s.contains("Qwen3 0.6B"), "{s}");
    h.shoot("filters");
    let s = h.key(b"x");
    assert!(s.contains("s Status: [All]") && s.contains("[ ] Fits this computer"), "{s}");
}
