//! Headless UI tests for the multimodal sandbox (Review screen) and the
//! docs assistant (F2) — web-console parity 2026-09-27. Own file (and a
//! minimal harness) so parallel parity branches never collide in
//! headless_ui.rs. No test touches the network: the worker is a dummy
//! channel and gateway results are applied to the store directly.

use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{
    ConnPhase, Identity, Loadable, ProvidersData, RoutesData, SandboxOutcome, Store,
};
use abstractgateway_console::ui::docs::{Role, Turn};
use abstractgateway_console::ui::sandbox::{ArtifactRef, MediaOutcome, SbMode};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

struct NoTransport;

impl ConsoleTransport for NoTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn engines_status(&self, _probe: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn models_catalog(
        &self,
        _q: &str,
        _e: Option<&str>,
        _f: bool,
    ) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn models_installed(&self, _p: Option<&str>) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn start_download(&self, _p: &str, _a: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn delete_model(&self, _p: &str, _a: &str, _f: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn engine_install(&self, _id: &str, _d: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn cancel_job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
}

struct H {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
}

fn harness(size: Size) -> H {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let store_slot: Rc<RefCell<Option<Store>>> = Rc::new(RefCell::new(None));
    let store_out = store_slot.clone();
    let ui_slot: Rc<RefCell<Option<UiState>>> = Rc::new(RefCell::new(None));
    let ui_out = ui_slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        *store_out.borrow_mut() = Some(store);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        *ui_out.borrow_mut() = Some(ui_state);
        let transport: Arc<dyn ConsoleTransport> = Arc::new(NoTransport);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
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
    let store = store_slot.borrow().expect("store");
    let ui = ui_slot.borrow().expect("ui");
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
    fn type_text(&mut self, s: &str) {
        self.term.push_input(s.as_bytes());
    }
    fn find_cmd(&mut self, mut pred: impl FnMut(&Cmd) -> bool) -> Option<Cmd> {
        while let Ok(c) = self.rx.try_recv() {
            if pred(&c) {
                return Some(c);
            }
        }
        None
    }
    fn connect(&mut self) {
        let id = Identity::from_me(&json!({
            "ok": true,
            "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "admin": true},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .expect("identity");
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
    }
    fn review(&mut self) {
        self.ui.wizard.set(false);
        self.ui.screen.set(6);
        self.turns(3);
    }
}

fn routes() -> RoutesData {
    RoutesData::from_value(&json!({
        "ok": true, "writable": true, "authority": "abstractcore.gateway_runtime", "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text", "label": "Text Input",
             "provider": "lmstudio", "model": "test-model-a", "configured": true},
            {"key": "output.voice", "kind": "output", "modality": "voice", "label": "Voice Output",
             "provider": "supertonic", "model": "supertonic-3", "options": {"voice": "M3"}, "configured": true},
            {"key": "output.image.text_to_image", "kind": "output", "modality": "image",
             "label": "Image Generation", "task": "text_to_image",
             "provider": "mlx-gen", "model": "test-flux", "configured": true},
            {"key": "output.music", "kind": "output", "modality": "music", "label": "Music Output",
             "source": "not_configured", "configured": false}
        ]
    }))
}

fn providers() -> ProvidersData {
    ProvidersData::from_value(&json!({
        "items": [{"name": "lmstudio", "display_name": "LMStudio", "status": "available",
                   "local_provider": true, "authentication_required": false, "models": []}]
    }))
}

/// Image mode sends the web's exact route + body on the shared sandbox
/// run, shows the in-flight state, then renders the saved artifact.
#[test]
fn image_mode_dispatches_the_web_route_and_renders_the_saved_file() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(routes()));
    h.store.sandbox_ws.mode.set(SbMode::Image);
    h.ui.sb_prompt.set("a red fox".into());
    h.review();
    let s = h.turns(2);
    assert!(
        s.contains("output.image.text_to_image will use mlx-gen / test-flux"),
        "the media route line names the configured pair:\n{s}"
    );
    assert!(
        s.contains("Image — test-flux"),
        "the mode picker shows the mode + model:\n{s}"
    );
    h.type_text("\r");
    h.turns(2);
    let req = match h.find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. })) {
        Some(Cmd::SandboxMedia { request }) => request,
        other => panic!("expected SandboxMedia, got {other:?}"),
    };
    assert_eq!(req.mode, SbMode::Image);
    assert_eq!(req.leaf, "images/generate");
    assert_eq!(
        req.run_id,
        "session_memory_gateway_console_sandbox_default_admin"
    );
    assert_eq!(req.body["prompt"], "a red fox");
    assert_eq!(req.body["image_provider"], "mlx-gen");
    assert_eq!(req.body["image_model"], "test-flux");
    assert!(req.body["request_id"]
        .as_str()
        .unwrap()
        .starts_with("sandbox_"));
    let s = h.turns(2);
    assert!(
        s.contains("⟳ generating"),
        "synchronous in-flight state:\n{s}"
    );
    // A second Enter while generating must not fire a second paid call.
    h.type_text("x\r");
    h.turns(2);
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. }))
        .is_none());

    h.store.sandbox_ws.media.set(Loadable::Ready(MediaOutcome {
        mode: SbMode::Image,
        provider: "mlx-gen".into(),
        model: "test-flux".into(),
        run_id: req.run_id.clone(),
        artifact: ArtifactRef {
            id: "art_42".into(),
            content_type: "image/png".into(),
            filename: None,
        },
        saved: Ok((PathBuf::from("/tmp/x/sandbox-image-art_42.png"), 2048)),
        image: Some(Arc::new(Bitmap::new(4, 4, Rgba::rgb(200, 30, 30)))),
        image_note: None,
        elapsed_ms: 12_300,
    }));
    let s = h.turns(2);
    assert!(
        s.contains("✓ Image · mlx-gen / test-flux"),
        "outcome names mode + pair:\n{s}"
    );
    assert!(s.contains("artifact art_42"), "artifact id shown:\n{s}");
    assert!(
        s.contains("/tmp/x/sandbox-image-art_42.png") && s.contains("2.0 KB"),
        "saved path + size shown:\n{s}"
    );
}

/// Voice mode carries the route's voice; an unconfigured / absent mode
/// refuses with the web's reason and fires nothing.
#[test]
fn voice_carries_route_voice_and_unconfigured_modes_refuse() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(routes()));
    h.store.sandbox_ws.mode.set(SbMode::Voice);
    h.ui.sb_prompt.set("hello there".into());
    h.review();
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. })) {
        Some(Cmd::SandboxMedia { request }) => {
            assert_eq!(request.leaf, "voice/tts");
            assert_eq!(
                request.body,
                json!({"text": "hello there", "provider": "supertonic", "model": "supertonic-3",
                       "voice": "M3", "request_id": request.body["request_id"]})
            );
        }
        other => panic!("expected SandboxMedia, got {other:?}"),
    }
    h.store.sandbox_ws.media.set(Loadable::NotAsked);

    h.store.sandbox_ws.mode.set(SbMode::Music);
    h.ui.sb_prompt.set("jazz".into());
    h.review();
    let s = h.turns(2);
    assert!(
        s.contains("Music — not configured"),
        "picker labels the gap:\n{s}"
    );
    h.type_text("jazz\r");
    h.turns(2);
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("output.music is not configured"),
        "names the refusal: {notice}"
    );
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. }))
        .is_none());

    h.store.sandbox_ws.mode.set(SbMode::Video);
    h.turns(2);
    h.type_text("waves\r");
    h.turns(2);
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("not offered by this gateway"),
        "absent route refusal: {notice}"
    );
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. }))
        .is_none());
}

/// Text turns ride the web body (no max_tokens) and the second turn
/// carries the first answered turn as `messages`.
#[test]
fn text_turns_send_the_web_body_with_history() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.store.providers.set(Loadable::Ready(providers()));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into()]),
        );
    });
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-a".into());
    h.ui.sb_prompt.set("first question".into());
    h.review();
    h.store.sandbox_ws.reasoning_ix.set(3); // low
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    let body = match h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. })) {
        Some(Cmd::SandboxTest { request, .. }) => request.0,
        other => panic!("expected SandboxTest, got {other:?}"),
    };
    assert_eq!(body["capability"], "input.text");
    assert_eq!(body["prompt"], "first question");
    assert_eq!(body["messages"], json!([]));
    assert_eq!(body["reasoning"], "low");
    assert!(
        body.get("max_tokens").is_none(),
        "the web sends no max_tokens"
    );

    h.store.sandbox.set(Loadable::Ready(SandboxOutcome {
        ok: true,
        error: None,
        response: "ANSWER-ONE".into(),
        routed_provider: Some("lmstudio".into()),
        profile: None,
        usage: None,
        provider: "lmstudio".into(),
        model: "test-model-a".into(),
    }));
    let s = h.turns(2);
    assert!(s.contains("ANSWER-ONE"), "answer renders:\n{s}");
    h.type_text("second question\r");
    h.turns(2);
    let body = match h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. })) {
        Some(Cmd::SandboxTest { request, .. }) => request.0,
        other => panic!("expected the second SandboxTest, got {other:?}"),
    };
    assert_eq!(body["prompt"], "second question");
    assert_eq!(
        body["messages"],
        json!([{"role": "user", "content": "first question"},
               {"role": "assistant", "content": "ANSWER-ONE"}])
    );
}

/// The media workspace fits 80x24 with a result (no content fused into
/// a border), like the text workspace's pinned matrix.
#[test]
fn media_mode_renders_whole_at_80x24() {
    let mut h = harness(Size::new(80, 24));
    h.connect();
    h.store.routes.set(Loadable::Ready(routes()));
    h.store.sandbox_ws.mode.set(SbMode::Voice);
    h.store.sandbox_ws.media.set(Loadable::Ready(MediaOutcome {
        mode: SbMode::Voice,
        provider: "supertonic".into(),
        model: "supertonic-3".into(),
        run_id: "r".into(),
        artifact: ArtifactRef {
            id: "a1".into(),
            content_type: "audio/wav".into(),
            filename: None,
        },
        saved: Ok((PathBuf::from("/tmp/v.wav"), 90_000)),
        image: None,
        image_note: None,
        elapsed_ms: 900,
    }));
    h.review();
    let s = h.turns(3);
    assert!(
        s.contains("✓ Voice · supertonic / supertonic-3"),
        "outcome:\n{s}"
    );
    assert!(s.contains("/tmp/v.wav"), "saved path:\n{s}");
    for l in s.lines() {
        if l.trim_start().starts_with('╰') {
            assert!(
                !l.chars().any(|c| c.is_ascii_alphanumeric()),
                "fused: {l:?}\n{s}"
            );
        }
    }
}

/// The header advertises F2 only when connected (the web's ✦ button is
/// session-only).
#[test]
fn header_advertises_docs_assistant_when_connected() {
    let mut h = harness(Size::new(110, 34));
    let s = h.turns(2);
    assert!(
        !s.contains("F2 docs assistant"),
        "not advertised disconnected:\n{s}"
    );
    h.connect();
    let s = h.turns(2);
    assert!(
        s.contains("F2 docs assistant"),
        "advertised when connected:\n{s}"
    );
}

/// F2 = the docs assistant: refused (with a reason) while disconnected;
/// connected it opens, asks through DocsAsk, and renders the answer.
#[test]
fn f2_docs_assistant_asks_and_renders() {
    let mut h = harness(Size::new(110, 34));
    h.turns(2);
    h.type_text("\x1bOQ"); // F2
    h.turns(2);
    let notice = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        notice.contains("docs assistant needs a gateway connection"),
        "{notice}"
    );

    h.connect();
    h.type_text("\x1bOQ");
    let s = h.turns(3);
    assert!(s.contains("Docs assistant"), "modal opens:\n{s}");
    assert!(
        s.contains("grounded on the gateway's own documentation"),
        "note:\n{s}"
    );
    h.type_text("how do I add a provider?\r");
    let s = h.turns(3);
    match h.find_cmd(|c| matches!(c, Cmd::DocsAsk { .. })) {
        Some(Cmd::DocsAsk {
            question, history, ..
        }) => {
            assert_eq!(question, "how do I add a provider?");
            assert!(history.is_empty());
        }
        other => panic!("expected DocsAsk, got {other:?}"),
    }
    assert!(s.contains("Thinking"), "pending turn renders:\n{s}");
    // A second ask while one is in flight is refused.
    h.type_text("again\r");
    h.turns(2);
    assert!(h.find_cmd(|c| matches!(c, Cmd::DocsAsk { .. })).is_none());
    // The worker settles the pending turn (docs::after_poll's effect).
    h.store.docs.turns.update(|t| {
        let last = t.last_mut().unwrap();
        assert_eq!(last.role, Role::Pending);
        *last = Turn {
            role: Role::Assistant,
            text: "Open **Providers** (screen 2).".into(),
        };
    });
    h.store.docs.busy.set(false);
    let s = h.turns(2);
    assert!(
        s.contains("Open") && s.contains("Providers"),
        "answer renders:\n{s}"
    );
}
