//! abstractgateway-console — a keyboard-first terminal wizard to
//! configure AbstractGateway (connection, providers, multimodal
//! capability routes, users & entities), rendered by AbstractTUI
//! against the gateway's existing admin HTTP API.
//!
//! Screens 9 (Models) and 0 (Engines) are AbstractCore's shared screens
//! from the `abstractcore-console` crate, answered by
//! [`transport_http::HttpTransport`] over the gateway's
//! `/api/gateway/models/*` and `/engines/*` mirrors.
//!
//! Architecture: the UI thread owns all signals; one worker thread owns
//! the HTTP client (and publishes the verified one to the shared
//! screens' own worker through a [`transport_http::ClientSlot`]). Commands cross via mpsc, results come back as
//! closures posted through `WakeHandle` (the engine's live-data law).

pub mod api;
pub mod health;
/// The one query language every search box speaks (substring, or glob).
pub mod query;
pub mod store;
/// The shared Models/Engines screens' transport over the gateway API.
pub mod transport_http;
pub mod ui;
pub mod worker;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::mpsc;

use abstracttui::prelude::*;

use store::Store;
use ui::{Ctx, UiState};

const HELP: &str = "\
abstractgateway-console — configure an AbstractGateway from the terminal

USAGE:
  abstractgateway-console [--url URL] [--token TOKEN] [--wizard|--browse]
                          [--theme ID]

OPTIONS:
  --url URL      gateway base URL (default http://127.0.0.1:8080)
  --token TOKEN  bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN;
                 prefer the env var — argv is visible in `ps`)
  --wizard       start in the guided wizard (default)
  --browse       start in browse mode (tabs, no step gating)
  --theme ID     abstracttui theme id (also $ABSTRACTTUI_THEME)
  -h, --help     this help
  --version      print the version

KEYS: Tab focus · Enter activate · Ctrl+N next step · Ctrl+P / Esc back ·
      ] / [ next/back (outside text fields) · 1-9,0 screens (browse) ·
      r refresh · Ctrl+L repaint · q / Ctrl+C quit

SCREENS: 1 Connection · 2 Providers · 3 Routes · 4 Users & Entities ·
         5 Runtimes · 6 Workflows · 7 Review & Test · 8 Resources ·
         9 Models (browse, download, delete models on the gateway host) ·
         0 Engines (detect and install Ollama, LM Studio, MLX, llama.cpp)
";

struct Args {
    url: String,
    token: String,
    wizard: bool,
    theme: Option<String>,
}

fn parse_args(argv: &[String]) -> Result<Option<Args>, String> {
    let mut url = String::new();
    let mut token = String::new();
    let mut wizard = true;
    let mut theme = None;
    let mut it = argv.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "-h" | "--help" => {
                println!("{HELP}");
                return Ok(None);
            }
            "--version" => {
                println!("abstractgateway-console {}", env!("CARGO_PKG_VERSION"));
                return Ok(None);
            }
            "--url" => url = it.next().cloned().ok_or("--url needs a value")?,
            "--token" => token = it.next().cloned().ok_or("--token needs a value")?,
            "--wizard" => wizard = true,
            "--browse" => wizard = false,
            "--theme" => theme = Some(it.next().cloned().ok_or("--theme needs a value")?),
            other => return Err(format!("unknown argument: {other} (see --help)")),
        }
    }
    Ok(Some(Args {
        url,
        token,
        wizard,
        theme,
    }))
}

/// CLI entry — returns the process exit code.
pub fn run_cli(argv: &[String]) -> i32 {
    let args = match parse_args(argv) {
        Ok(Some(a)) => a,
        Ok(None) => return 0,
        Err(e) => {
            eprintln!("abstractgateway-console: {e}");
            return 2;
        }
    };

    // Headless guard (CI / piped runs): skip cleanly, exit 0.
    if !abstracttui::term::have_tty() {
        println!("abstractgateway-console: needs an interactive terminal — skipping cleanly");
        return 0;
    }

    if let Some(id) = args
        .theme
        .clone()
        .or_else(|| std::env::var("ABSTRACTTUI_THEME").ok())
    {
        set_theme_by_id(&id);
    }

    let env_token_set = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN")
        .map(|t| !t.trim().is_empty())
        .unwrap_or(false);

    let mut app = App::new(Size::new(110, 32));
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<worker::Cmd>();

    let url0 = if args.url.is_empty() {
        "http://127.0.0.1:8080".to_string()
    } else {
        args.url.clone()
    };
    let token0 = args.token.clone();
    let wizard0 = args.wizard;

    let store_slot: Rc<RefCell<Option<Store>>> = Rc::new(RefCell::new(None));
    let store_out = store_slot.clone();
    let ui_slot: Rc<RefCell<Option<UiState>>> = Rc::new(RefCell::new(None));
    let ui_out = ui_slot.clone();

    // The Models/Engines screens' transport reads the client the worker
    // verified (published into this slot on every successful probe).
    let client_slot = transport_http::client_slot();
    let screens_transport: std::sync::Arc<dyn abstractcore_console::ConsoleTransport> =
        std::sync::Arc::new(transport_http::HttpTransport::new(
            client_slot.clone(),
            ui::normalize_url(&url0),
        ));
    let overlays_screens = overlays.clone();

    let tx_mount = tx.clone();
    if let Err(e) = app.mount(move |cx| {
        let store = Store::create(cx);
        *store_out.borrow_mut() = Some(store);
        let ui_state = UiState::create(cx, url0.clone(), token0.clone());
        ui_state.wizard.set(wizard0);
        *ui_out.borrow_mut() = Some(ui_state);
        let prober: ui::ProberSlot = Rc::new(RefCell::new(None));
        // Production prober: ONE short-lived thread per verification,
        // FRESH client from the same credential resolution the Probe
        // button uses — never through the worker queue (must not wait
        // behind a 300s test), never sharing the worker's client. The
        // settle posts back through the wake handle; health::settle
        // discards stale generations.
        {
            let wake_probe = abstracttui::reactive::wake_handle();
            let tx_probe = tx_mount.clone();
            *prober.borrow_mut() = Some(Box::new(
                move |url: String, token: Option<String>, gen: u64| {
                    let wake = wake_probe.clone();
                    let tx = tx_probe.clone();
                    std::thread::spawn(move || {
                        let client = crate::api::GatewayClient::new(&url, token.as_deref());
                        let outcome = client.ping().map(|_| ());
                        wake.post(move || crate::health::settle(store, &tx, gen, outcome));
                    });
                },
            ));
        }
        // ONE ScreensCtx, created in the mount scope, sharing this app's
        // notice signal (its toast effect + footer already render it).
        let screens = abstractcore_console::screens::ScreensCtx::new(
            cx,
            screens_transport.clone(),
            overlays_screens.clone(),
            abstractcore_console::screens::ScreensOptions {
                notice: Some(store.notice),
                ..abstractcore_console::screens::ScreensOptions::default()
            },
        );
        let ctx = Ctx {
            tx: tx_mount.clone(),
            overlays: overlays.clone(),
            quitter: quitter.clone(),
            store,
            ui: ui_state,
            modal: Rc::new(RefCell::new(None)),
            entity_drawer: Rc::new(RefCell::new(None)),
            env_token_set,
            prober,
            screens,
            screens_transport: screens_transport.clone(),
        };
        ui::root(cx, ctx)
    }) {
        eprintln!("abstractgateway-console: mount failed: {e}");
        return 1;
    }

    // Worker thread: owns the HTTP client; posts results to the UI thread.
    let wake = abstracttui::reactive::wake_handle();
    let store = store_slot.borrow().expect("store created");
    let ui_state = ui_slot.borrow().expect("ui state created");
    let token_sink = {
        let wake = wake.clone();
        move |user: String, token: String| {
            let ui = ui_state;
            wake.post(move || {
                ui.token_queue
                    .update(|q| q.push((user.clone(), token.clone())))
            });
        }
    };
    let done_sink = {
        let wake = wake.clone();
        move |form_id: u64, outcome: Result<String, String>| {
            let ui = ui_state;
            wake.post(move || ui.write_done.set(Some((form_id, outcome.clone()))));
        }
    };
    let worker_handle = worker::spawn_with_client_slot(
        store,
        wake,
        rx,
        tx.clone(),
        client_slot,
        token_sink,
        done_sink,
    );

    // Auto-probe at boot ALWAYS (P2-B, cycle-1 UX) — even tokenless: a
    // local dev gateway with open reads is then a ZERO-keystroke
    // connect, and an auth gateway shows its 401 panel immediately
    // (whose copy already teaches the fix) instead of a neutral "not
    // connected" the operator must act on blindly. Through the SAME
    // normalization + token-source bookkeeping the Probe button uses
    // (they must never disagree); a tokenless boot sends the honest
    // "none — no Authorization header sent" source.
    {
        let url = ui::normalize_url(&ui_state.conn_url.get_untracked());
        ui_state.conn_url.set(url.clone());
        let (token, source) = if !args.token.is_empty() {
            (
                args.token.clone(),
                format!("--token flag ({} chars)", args.token.chars().count()),
            )
        } else if env_token_set {
            let t = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN")
                .unwrap_or_default()
                .trim()
                .to_string();
            let d = format!(
                "env ABSTRACTGATEWAY_AUTH_TOKEN ({} chars)",
                t.chars().count()
            );
            (t, d)
        } else {
            (
                String::new(),
                "none — no Authorization header sent".to_string(),
            )
        };
        ui_state.token_source.set(Some(source));
        let _ = tx.send(worker::Cmd::Connect {
            url,
            token: token.into(),
        });
    }

    // hover_ink arms mode 1003 (motion with no button held) so the 50-odd
    // Buttons across the six screens actually light under the pointer —
    // their hover visuals exist in the engine but never fire without it.
    // Table has no hover state, so the inventory tables stay inert.
    // platform_clipboard stays default-true: this is the interactive
    // binary, and the token copy wants the host clipboard when the
    // terminal does not advertise OSC 52.
    let result = app.run_with(RunConfig {
        hover_ink: true,
        ..RunConfig::default()
    });
    // Drop the sender so an idle worker unblocks and ends. Deliberately
    // NO join: a worker mid-HTTP (slow agent reads up to 300s) would
    // hang quit with the terminal already restored — process exit reaps
    // the thread, and every call is client-side idempotent-safe to
    // abandon (reads, or writes the gateway completes server-side).
    drop(tx);
    drop(worker_handle);

    match result {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("abstractgateway-console: {e}");
            1
        }
    }
}
