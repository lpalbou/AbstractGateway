//! OpenAI API page (R7.2) driven through the REAL worker against a LIVE
//! hermetic gateway: each action's key press, then the gateway's STATE
//! read back by a direct HTTP GET (the same route the web page reads).
//! Ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18781 ABSTRACTGATEWAY_AUTH_TOKEN=<admin> \
//!   R7W2_ALICE_TOKEN=r7w2-alice-token-0001 \
//!   cargo test --test live_r7w2_openai -- --ignored --test-threads 1
//!
//! The scratch gateway: untracked/round4/r7/w2/run_scratch_gateway*.sh +
//! seed.py (alice, bob with the OpenAI API off, a few /v1 requests).

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

struct H {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    _worker: std::thread::JoinHandle<()>,
}

fn live(token: &str) -> H {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    abstracttui::app::set_theme_by_id("abstract-dark");
    let size = Size::new(120, 60);
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
    let (store, ui_state) = slot.borrow().expect("created");
    let wake = abstracttui::reactive::wake_handle();
    let done_sink = {
        let wake = wake.clone();
        move |fid: u64, out: Result<String, String>| {
            wake.post(move || ui_state.write_done.set(Some((fid, out.clone()))))
        }
    };
    let worker = abstractgateway_console::worker::spawn(
        store,
        wake,
        rx,
        tx_keep.clone(),
        |_u: String, _t: String| {},
        done_sink,
    );
    let mut h = H {
        app,
        term,
        driver,
        store,
        ui: ui_state,
        _worker: worker,
    };
    h.ui.wizard.set(false);
    h.ui.conn_url.set(url.clone());
    h.ui.conn_token.set(token.to_string());
    tx_keep
        .send(Cmd::Connect {
            url,
            token: token.to_string().into(),
        })
        .unwrap();
    h.until("connected", |h, _| {
        h.store.conn.with_untracked(ConnPhase::is_connected)
    });
    h.ui.screen.set(ui::SCREEN_OPENAI);
    h.until("the page", |_, s| {
        s.contains("One address for every OpenAI-compatible app.")
    });
    h
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
    fn until(&mut self, what: &str, mut pred: impl FnMut(&mut H, &str) -> bool) -> String {
        let deadline = Instant::now() + Duration::from_secs(20);
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
            std::thread::sleep(Duration::from_millis(30));
        }
    }
}

fn get(path: &str, token: &str) -> Result<Value, u16> {
    let url = std::env::var("ABSTRACTGATEWAY_URL").unwrap();
    match ureq::get(&format!("{url}/api/gateway{path}"))
        .set("Authorization", &format!("Bearer {token}"))
        .call()
    {
        Ok(r) => Ok(serde_json::from_str(&r.into_string().unwrap()).unwrap()),
        Err(ureq::Error::Status(code, _)) => Err(code),
        Err(e) => panic!("{e}"),
    }
}

/// The page said the web page's saved sentence (the write landed and the
/// page is idle again).
fn saved(h: &H, sentence: &str) -> bool {
    // R15: a verified success is a toast (the card keeps the sentence too).
    h.store.notice.get_untracked().as_deref() == Some(sentence)
        || abstractgateway_console::ui::w::notify::toasts()
            .last()
            .is_some_and(|t| t == sentence)
}

/// In an open choice prompt (initial = the gateway's selected option),
/// move to `target` among the shown options and press Enter.
fn pick(h: &mut H, field: &str, target: &str) {
    let opts: Vec<Value> = state(field)
        .as_array()
        .unwrap()
        .iter()
        .filter(|o| o.get("shown") != Some(&json!(false)))
        .cloned()
        .collect();
    let at = opts
        .iter()
        .position(|o| o["selected"] == json!(true))
        .unwrap_or(0) as i64;
    let to = opts
        .iter()
        .position(|o| o["id"] == json!(target))
        .expect("target") as i64;
    for _ in 0..(to - at).abs() {
        h.key(if to > at { b"\x1b[B" } else { b"\x1b[A" });
    }
    h.key(b"\r");
}

fn admin() -> String {
    std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN")
}

fn state(field: &str) -> Value {
    get("/openai-api", &admin()).expect("GET /openai-api")[field].clone()
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn admin_actions_change_the_gateway_like_the_web_page() {
    let mut h = live(&admin());
    assert_eq!(state("enabled"), json!(true), "seeded endpoint on");

    // Endpoint off, then on (e).
    h.key(b"e");
    h.until("endpoint off", |h, _| {
        saved(h, "Stopped: open requests ended.")
    });
    assert_eq!(state("enabled"), json!(false));
    h.key(b"e");
    h.until("endpoint on", |h, _| {
        saved(h, "Running: apps can connect now.")
    });
    assert_eq!(state("enabled"), json!(true));

    // Authentication: Open, run as alice, back to Guest, then Protected
    // (a run that died midway left Open on: start from Protected).
    if state("access") == json!("open") {
        h.key(b"a");
        h.until("protected", |h, _| {
            saved(h, "Saved: Protected (API key). Applies now.")
        });
    }
    h.key(b"a");
    h.until("open mode", |h, _| {
        saved(h, "Saved: Open (no key). Applies now.")
    });
    assert_eq!(state("access"), json!("open"));
    // Run as: to the other of alice / Guest, then back.
    let start = state("open_account");
    let (first, back) = if start == json!("alice") {
        ("guest", "alice")
    } else {
        ("alice", "guest")
    };
    for target in [first, back] {
        h.key(b"u");
        h.until("the run-as prompt", |_, s| {
            s.contains("never run as an admin")
        });
        pick(&mut h, "open_account_options", target);
        let label = if target == "guest" {
            "Guest (models only)"
        } else {
            "alice"
        };
        let sentence = format!("Saved: requests without a key run as {label}.");
        h.until("run as", |h, _| saved(h, &sentence));
        assert_eq!(state("open_account"), json!(target));
    }
    h.key(b"a");
    h.until("protected", |h, _| {
        saved(h, "Saved: Protected (API key). Applies now.")
    });
    assert_eq!(state("access"), json!("token"));

    // Who can connect: Devices on my network, then back to This machine only.
    h.key(b"w");
    h.until("the reach prompt", |_, s| {
        s.contains("Devices on my network")
    });
    pick(&mut h, "reach_options", "network");
    h.until("reach network", |h, _| {
        saved(h, "Saved: Devices on my network. Applies now.")
    });
    assert_eq!(state("reach"), json!("network"));
    h.key(b"w");
    h.until("the reach prompt", |_, s| s.contains("This machine only"));
    pick(&mut h, "reach_options", "machine");
    h.until("reach machine", |h, _| {
        saved(h, "Saved: This machine only. Applies now.")
    });
    assert_eq!(state("reach"), json!("machine"));

    // Check setup and Restart answer on the page.
    h.key(b"h");
    h.until("the checks", |_, s| {
        s.contains("OK   ") || s.contains("Fix  ") || s.contains("Note ")
    });
    h.key(b"x");
    h.until("restarted", |_, s| s.contains("Restarted: "));
    assert_eq!(state("running"), json!(true));

    // A log row opens to its recorded request (the same record route).
    let rows = get("/openai-api/logs?limit=25", &admin()).unwrap()["rows"]
        .as_array()
        .cloned()
        .unwrap();
    assert!(!rows.is_empty(), "seeded requests");
    // R15: Enter on the request table opens the record (a modal).
    h.key(b"\r");
    h.until("the record", |_, s| {
        s.contains("Keys and tokens were removed")
    });
    // Reveal shows the admin token this console signed in with (Esc
    // closes the record first).
    h.term.push_input(&[0x1b]);
    h.turns(1);
    std::thread::sleep(Duration::from_millis(45));
    h.turns(3);
    let s = h.key(b"v");
    assert!(s.contains(&admin()), "revealed key:\n{s}");
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn a_user_sees_own_requests_and_new_key_replaces_the_token() {
    let alice = std::env::var("R7W2_ALICE_TOKEN").expect("R7W2_ALICE_TOKEN");
    let mut h = live(&alice);
    let s = h.until("own requests", |_, s| {
        s.contains("Your requests, newest first.")
    });
    assert!(s.contains("Only an admin can start or stop it."), "{s}");
    assert!(!s.contains("bob "), "only alice's rows:\n{s}");
    // New key: confirm, then the old token stops working, the new one is
    // the console's, and the page reads again with it.
    h.key(b"n");
    let s = h.until("the confirm", |_, s| s.contains("Make a new key?"));
    // R15 F1: answered by its [New key] button.
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(" New key ") && l.contains(" Cancel "))
        .last()
        .unwrap_or_else(|| panic!("[New key] [Cancel]:\n{s}"));
    let x = line[..line.rfind(" New key ").unwrap() + 1].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.until("the new key", |h, _| {
        h.ui.conn_token.get_untracked() != alice
    });
    let fresh = h.ui.conn_token.get_untracked();
    assert_eq!(
        get("/openai-api", &alice).unwrap_err(),
        401,
        "old key refused"
    );
    assert_eq!(
        get("/openai-api", &fresh).unwrap()["key"]["user_id"],
        json!("alice")
    );
    let s = h.until("reconnected, key shown", |h, s| {
        h.store.conn.with_untracked(ConnPhase::is_connected) && s.contains(&fresh)
    });
    assert!(s.contains("Your gateway token"), "{s}");
    // Print the new token for the operator's next run (the seed's is gone).
    eprintln!("alice's new token: {fresh}");
}
