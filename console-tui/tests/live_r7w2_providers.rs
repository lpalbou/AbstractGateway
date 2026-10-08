//! Providers (round 7, R7.2) through the REAL worker against a LIVE
//! hermetic gateway (untracked/round4/r7/w2/run_scratch_gateway.sh: fake
//! Ollama + fake OpenAI upstream, scratch HOME, no keys). Ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18801 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   R7W2_SHOTS_DIR=<dir> cargo test --test live_r7w2_providers -- --ignored --test-threads 1
//!
//! Never a real engine install: the install plan is a DRY RUN (the web's
//! engineLoadPlans: "a dry run runs nothing"), then nothing is started.
//! Start/Stop are not driven live: on a real Mac they quit/launch the
//! person's own Ollama / LM Studio app (osascript / `lms`), which no test
//! may do — they are pinned headless (tests/r7w2_providers.rs).

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

use abstractgateway_console::store::{ConnPhase, Identity, Store};
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
    tx: mpsc::Sender<Cmd>,
    rx: Option<mpsc::Receiver<Cmd>>,
}

fn harness(size: Size) -> Harness {
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
        let Ok(dir) = std::env::var("R7W2_SHOTS_DIR") else {
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

fn live(size: Size) -> Harness {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN");
    let mut h = harness(size);
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
    h
}

fn engines_of(h: &Harness) -> Vec<Value> {
    match h.store.json.get_untracked("engines") {
        abstractgateway_console::store::Loadable::Ready(v) => v
            .get("engines")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
        _ => Vec::new(),
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn local_engines_render_the_scratch_hosts_engines() {
    for size in [Size::new(120, 40), Size::new(80, 24)] {
        let mut h = live(size);
        h.ui.screen.set(ui::SCREEN_PROVIDERS);
        let s = h.until("the engine rows", |h, _| !engines_of(h).is_empty());
        let _ = s;
        let s = h.turns(3);
        // Every engine GET /engines reports is a row, by its name.
        for e in engines_of(&h) {
            let name = e.get("name").and_then(Value::as_str).unwrap_or("");
            let first = name.split_whitespace().next().unwrap_or(name);
            assert!(s.contains(first), "{name} on screen:\n{s}");
        }
        assert!(
            s.contains("Engines that run models on this computer"),
            "{s}"
        );
        assert!(s.contains("installed"), "the summary line:\n{s}");
        h.shoot("live-providers-local");
        // R15: each row shows its card's sentences and links (no expansion).
        let s = h.turns(3);
        assert!(
            s.contains("Learn more") || s.contains("Browse models"),
            "the rows' own buttons:\n{s}"
        );
        h.shoot("live-providers-engine-expanded");
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn install_plan_is_a_dry_run_and_nothing_is_installed() {
    use abstractgateway_console::store::json::WriteState;
    use abstractgateway_console::ui::providers::engines::{confirm_lines, plan_key};
    use abstractgateway_console::worker::json::JsonCmd;
    let mut h = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_PROVIDERS);
    h.until("the engine rows", |h, _| !engines_of(h).is_empty());
    let before = engines_of(&h);
    let ollama = before
        .iter()
        .find(|e| e.get("id").and_then(Value::as_str) == Some("ollama"))
        .cloned()
        .expect("the scratch host reports Ollama");
    // The two requests the confirm sends (engineLoadPlans), verbatim.
    for loc in ["user", "system"] {
        h.tx.send(Cmd::Json(JsonCmd::Send {
            key: plan_key("ollama", loc),
            method: "POST".into(),
            path: "/engines/ollama/install".into(),
            body: json!({"dry_run": true, "location": loc}),
            slow: true,
            label: "Check where Ollama can go".into(),
            reload: Vec::new(),
            journal: false,
        }))
        .unwrap();
    }
    h.until("both plans", |h, _| {
        ["user", "system"].iter().all(|l| {
            matches!(
                h.store.json.write_untracked(&plan_key("ollama", l)),
                Some(WriteState::Done(_)) | Some(WriteState::Failed(_))
            )
        })
    });
    let user = h.store.json.write_untracked(&plan_key("ollama", "user"));
    let system = h.store.json.write_untracked(&plan_key("ollama", "system"));
    let lines = confirm_lines(&ollama, "this gateway", (user.as_ref(), system.as_ref()));
    assert_eq!(lines[0], "Install Ollama on this gateway?");
    assert!(
        lines
            .iter()
            .any(|l| l.starts_with("Install puts it in your own Applications folder"))
            || lines
                .iter()
                .any(|l| l == "Could not check the install locations."),
        "{lines:?}"
    );
    // Not now: nothing ran — the engines read back unchanged, no job.
    h.tx.send(Cmd::Json(JsonCmd::get("engines.jobs", "/engines/jobs")))
        .unwrap();
    let jobs = h.until("the job list", |h, _| {
        h.store.json.get_untracked("engines.jobs").ready().is_some()
    });
    let _ = jobs;
    let list = h.store.json.get_untracked("engines.jobs");
    let active = list
        .ready()
        .and_then(|v| v.get("jobs"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter(|j| {
                    matches!(
                        j.get("state").and_then(Value::as_str),
                        Some("queued" | "downloading" | "installing")
                    )
                })
                .count()
        })
        .unwrap_or(0);
    assert_eq!(active, 0, "a dry run starts no job");
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn a_remote_connection_is_added_from_its_preset_and_deleted() {
    let mut h = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_PROVIDERS);
    h.turns(3);
    h.until("the presets", |_, s| s.contains("Portkey"));
    h.until("the profiles", |h, _| {
        h.store.profiles.with_untracked(|p| p.ready().is_some())
    });
    h.shoot("live-providers-remote");
    // Custom OpenAI-compatible: its Configure button opens the form prefilled.
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("Custom OpenAI-compatible") && l.contains("Configure"))
        .unwrap_or_else(|| panic!("the preset row:\n{s}"));
    let x = line[..line.rfind("Configure").unwrap()].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    let s = h.until("the form", |_, s| {
        s.contains("Configure Custom OpenAI-compatible")
    });
    assert!(
        s.contains("custom-endpoint"),
        "the family's default id:\n{s}"
    );
    let upstream =
        std::env::var("R7W2_UPSTREAM").unwrap_or_else(|_| "http://127.0.0.1:18803/v1".into());
    // The Base URL field (click it), then ✓ Confirm.
    h.click_text("optional; leave blank for provider default");
    h.key(upstream.as_bytes());
    h.turns(2);
    h.click_text("✓ Confirm");
    h.until("custom-endpoint saved", |h, _| {
        h.store.profiles.with_untracked(|p| {
            p.ready()
                .map(|d| d.profiles.iter().any(|p| p.id == "custom-endpoint"))
                .unwrap_or(false)
        })
    });
    // The preset says it is connected now (fresh read).
    let s = h.until("Connected", |_, s| s.contains("Connected · no key"));
    let _ = s;
    // Available Providers: select it, d, Delete endpoint.
    let idx = h
        .store
        .profiles
        .with_untracked(|p| {
            p.ready()
                .and_then(|d| d.profiles.iter().position(|p| p.id == "custom-endpoint"))
        })
        .expect("row");
    h.ui.profile_sel.set(idx);
    let s = h.turns(3);
    assert!(s.contains("endpoint:custom-endpoint"), "{s}");
    h.shoot("live-providers-available");
    h.key(b"d");
    h.until("the delete confirm", |_, s| {
        s.split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .contains("Delete endpoint:custom-endpoint?")
    });
    h.key(b"\x1b[Z"); // Shift+Tab to the action button (R15 F1)
    h.turns(1);
    h.key(b"\r");
    h.until("custom-endpoint gone", |h, _| {
        h.store.profiles.with_untracked(|p| {
            p.ready()
                .map(|d| !d.profiles.iter().any(|p| p.id == "custom-endpoint"))
                .unwrap_or(false)
        })
    });
}
