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
    fn start_download(
        &self,
        _p: &str,
        _a: &str,
        _expected_bytes: Option<u64>,
    ) -> Result<Value, TransportError> {
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
            cx.signal(abstractcore_console::screens::Access::Admin),
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
        self.focus_prompt();
    }
    /// R15: a synthesized mouse click on the first `text` on screen.
    fn click(&mut self, text: &str) -> String {
        let s = self.turns(1);
        let (row, col) = s
            .lines()
            .enumerate()
            .find_map(|(i, l)| l.find(text).map(|c| (i, l[..c].chars().count())))
            .unwrap_or_else(|| panic!("{text:?} not on screen:\n{s}"));
        let (x, y) = (col + 2, row + 1);
        self.type_text(&format!("\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m"));
        self.turns(3)
    }
    /// The prompt never autofocuses (REVIEW-1 M1: a parked caret ate the
    /// screen keys, and the mode switch re-mounts it): put the caret there
    /// the way an operator does, with a click on its field column.
    fn focus_prompt(&mut self) {
        let s = self.turns(1);
        let (row, line) = s
            .lines()
            .enumerate()
            .find(|(_, l)| l.contains("│prompt "))
            .expect("prompt row on screen");
        let row = row + 1;
        // R15: the field column is found on screen (a nav rail may sit left).
        let x = line
            .chars()
            .collect::<String>()
            .find("│prompt ")
            .map(|b| line[..b].chars().count())
            .unwrap_or(0)
            + 25;
        self.type_text(&format!("\x1b[<0;{x};{row}M\x1b[<0;{x};{row}m"));
        self.turns(2);
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
        s.contains("Image will use mlx-gen / test-flux."),
        "the media route line names the configured pair:\n{s}"
    );
    // R15: the Output segment's tooltip is the web's "<mode>: <pair>".
    assert!(s.contains(" Image "), "the Image segment:\n{s}");
    assert_eq!(
        abstractgateway_console::ui::sandbox::mode_tips(Some(&routes().rows))[1],
        "Image: mlx-gen / test-flux"
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
        s.contains("Music is not configured yet."),
        "the route line names the gap:\n{s}"
    );
    assert_eq!(
        abstractgateway_console::ui::sandbox::mode_tips(Some(&routes().rows))[3],
        "Music: not configured"
    );
    h.type_text("jazz\r");
    h.turns(2);
    // R15: refusals sit inline under the action bar (adversary note c).
    let notice = abstractgateway_console::ui::sandbox::refusal_now().unwrap_or_default();
    assert!(
        notice.contains("output.music is not configured"),
        "names the refusal: {notice}"
    );
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. }))
        .is_none());

    h.store.sandbox_ws.mode.set(SbMode::Video);
    h.turns(2);
    h.focus_prompt();
    h.type_text("waves\r");
    h.turns(2);
    let notice = abstractgateway_console::ui::sandbox::refusal_now().unwrap_or_default();
    assert!(
        notice.contains("not offered by this gateway"),
        "absent route refusal: {notice}"
    );
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. }))
        .is_none());
}

/// Fresh-install shape: only `output.video` is set and the task row is
/// empty. The server resolves the task row to its parent, so the lane
/// is READY, says where the pair comes from, and sends the parent's pair.
#[test]
fn video_task_row_inherits_the_parent_like_the_server() {
    let mut h = harness(Size::new(130, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(RoutesData::from_value(&json!({
        "ok": true, "writable": true, "authority": "abstractcore.gateway_runtime", "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text", "label": "Text Input",
             "provider": "lmstudio", "model": "test-model-a", "configured": true},
            {"key": "output.video", "kind": "output", "modality": "video", "label": "Video Output",
             "provider": "mlx-gen", "model": "test-ltx", "configured": true,
             "task_keys": ["output.video.text_to_video"]},
            {"key": "output.video.text_to_video", "kind": "output", "modality": "video",
             "label": "Video Generation", "task": "text_to_video", "source": "not_configured",
             "configured": false, "broad_key": "output.video", "inherits_broad": true}
        ]
    }))));
    h.store.sandbox_ws.mode.set(SbMode::Video);
    h.ui.sb_prompt.set("waves at dusk".into());
    h.review();
    let s = h.turns(2);
    assert!(
        s.contains("Video will use mlx-gen / test-ltx (inherited from output.video)."),
        "the route line names the inherited pair and its source:\n{s}"
    );
    let rows = h
        .store
        .routes
        .with_untracked(|r| r.ready().unwrap().rows.clone());
    assert_eq!(
        abstractgateway_console::ui::sandbox::mode_tips(Some(&rows))[5],
        "Video: mlx-gen / test-ltx",
        "the Video segment's tooltip names the ready pair"
    );
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SandboxMedia { .. })) {
        Some(Cmd::SandboxMedia { request }) => {
            assert_eq!(request.leaf, "videos/generate");
            assert_eq!(request.body["video_provider"], "mlx-gen");
            assert_eq!(request.body["video_model"], "test-ltx");
        }
        other => panic!("expected SandboxMedia, got {other:?}"),
    }
}

/// A task row carrying settings but no provider+model stops resolution
/// (Core does not fall through to the parent): NOT ready, says why, and
/// fires nothing.
#[test]
fn partial_task_row_stops_resolution_and_refuses() {
    let mut h = harness(Size::new(130, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(RoutesData::from_value(&json!({
        "ok": true, "writable": true, "authority": "abstractcore.gateway_runtime", "errors": [],
        "routes": [
            {"key": "output.image", "kind": "output", "modality": "image", "label": "Image Output",
             "provider": "mlx-gen", "model": "test-flux", "configured": true,
             "task_keys": ["output.image.text_to_image"]},
            {"key": "output.image.text_to_image", "kind": "output", "modality": "image",
             "label": "Image Generation", "task": "text_to_image", "options": {"steps": 8},
             "configured": true, "broad_key": "output.image"}
        ]
    }))));
    h.store.sandbox_ws.mode.set(SbMode::Image);
    h.ui.sb_prompt.set("a fox".into());
    h.review();
    let s = h.turns(2);
    let rows = h
        .store
        .routes
        .with_untracked(|r| r.ready().unwrap().rows.clone());
    assert_eq!(
        abstractgateway_console::ui::sandbox::mode_tips(Some(&rows))[1],
        "Image: not configured",
        "the Image segment says it cannot run"
    );
    assert!(
        s.contains("output.image.text_to_image is not ready"),
        "route line says why:\n{s}"
    );
    h.type_text("\r");
    h.turns(2);
    // R15: refusals sit inline under the action bar (adversary note c).
    let notice = abstractgateway_console::ui::sandbox::refusal_now().unwrap_or_default();
    assert!(
        notice.contains("has settings but no provider + model"),
        "refusal names the reason: {notice}"
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
    assert!(!s.contains("✦ Docs"), "not advertised disconnected:\n{s}");
    h.connect();
    let s = h.turns(2);
    assert!(s.contains("✦ Docs"), "advertised when connected:\n{s}");
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
    // R15: the kit's right-edge drawer (title, head buttons, footer).
    assert!(s.contains("Docs assistant"), "drawer opens:\n{s}");
    assert!(
        s.contains("Past conversations") && s.contains("New conversation"),
        "head buttons:\n{s}"
    );
    assert!(
        s.contains("Grounded on AbstractGateway’s documentation"),
        "footer:\n{s}"
    );
    h.type_text("how do I add a provider?\r");
    let s = h.turns(3);
    match h.find_cmd(|c| matches!(c, Cmd::DocsAsk { .. })) {
        Some(Cmd::DocsAsk {
            question,
            session_id,
            ..
        }) => {
            assert_eq!(question, "how do I add a provider?");
            // The conversation's own session: the gateway replays its turns.
            assert_eq!(session_id, h.store.docs.session_id.get_untracked());
            assert!(
                session_id.starts_with("gateway-docs-assistant:"),
                "{session_id}"
            );
        }
        other => panic!("expected DocsAsk, got {other:?}"),
    }
    assert!(s.contains("Answering…"), "pending turn renders:\n{s}");
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
    h.store
        .docs
        .replay
        .set("Earlier messages not replayed: 49 (~61,234 tokens).".into());
    let s = h.turns(2);
    assert!(
        s.contains("Open") && s.contains("Providers"),
        "answer renders:\n{s}"
    );
    assert!(
        s.contains("Earlier messages not replayed: 49"),
        "replay note renders:\n{s}"
    );
    // New conversation: a new session, an empty thread, no replay note.
    let before = h.store.docs.session_id.get_untracked();
    h.store.docs.new_conversation();
    assert_ne!(h.store.docs.session_id.get_untracked(), before);
    assert!(h.store.docs.turns.get_untracked().is_empty());
    assert!(h.store.docs.replay.get_untracked().is_empty());
}

fn text_ready(h: &mut H) {
    h.store.providers.set(Loadable::Ready(providers()));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into()]),
        );
    });
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-a".into());
}

/// Attach: the dialog (Tab to Attach…, Enter) uploads the typed path on
/// the sandbox SESSION id (the route maps it onto the sandbox run — not
/// the web's doubled `session_memory_session_memory_…`); pending
/// attachments ride the next turn and a files-only turn gets the web's
/// default prompt.
#[test]
fn attachments_upload_on_the_session_and_ride_the_next_turn() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(routes()));
    text_ready(&mut h);
    h.review();
    // R15: the Attach button, by mouse.
    let s = h.click(" Attach ");
    assert!(
        s.contains("Attach a file to the next text turn"),
        "attach dialog opens:\n{s}"
    );
    h.type_text("~/Pictures/fox.png\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SandboxAttach { .. })) {
        Some(Cmd::SandboxAttach { path, session_id }) => {
            assert_eq!(path, "~/Pictures/fox.png");
            assert_eq!(session_id, "gateway_console_sandbox_default_admin");
        }
        other => panic!("expected SandboxAttach, got {other:?}"),
    }
    let s = h.turns(1);
    assert!(s.contains("uploading"), "in-flight upload state:\n{s}");
    // The worker's publish_upload.
    abstractgateway_console::ui::sandbox::publish_upload(
        &h.store,
        Ok(abstractgateway_console::ui::sandbox::Attachment {
            name: "fox.png".into(),
            size: 2048,
            content_type: "image/png".into(),
            artifact: json!({"$artifact": "att_9", "content_type": "image/png"}),
        }),
    );
    let s = h.turns(2);
    assert!(
        s.contains("✓ attached fox.png (2.0 KB)"),
        "dialog confirms:\n{s}"
    );
    h.term.push_input(&[0x1b]);
    h.turns(1);
    std::thread::sleep(std::time::Duration::from_millis(45));
    let s = h.turns(2);
    assert!(
        s.contains("📎 fox.png (2.0 KB)") && s.contains("Attach (1)"),
        "chips + count:\n{s}"
    );
    // Files only: the web's default prompt, the ref in `attachments`.
    h.ui.sb_prompt.set(String::new());
    h.review();
    h.type_text("\r");
    h.turns(2);
    let body = match h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. })) {
        Some(Cmd::SandboxTest { request, .. }) => request.0,
        other => panic!("expected SandboxTest, got {other:?}"),
    };
    assert_eq!(body["prompt"], "Please analyze the attached file(s).");
    assert_eq!(
        body["attachments"],
        json!([{"$artifact": "att_9", "content_type": "image/png"}])
    );
    assert!(
        h.store.sandbox_ws.attachments.get_untracked().is_empty(),
        "sent = cleared"
    );
}

/// Speak: offered on a reply when output.voice is configured; it sends
/// the reply through the voice/tts lane on the sandbox run with the
/// route's voice.
#[test]
fn speak_sends_the_reply_through_the_voice_lane() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.store.routes.set(Loadable::Ready(routes()));
    text_ready(&mut h);
    h.review();
    let s = h.turns(2);
    assert!(!s.contains("Speak"), "no Speak without a reply:\n{s}");
    h.store.sandbox.set(Loadable::Ready(SandboxOutcome {
        ok: true,
        error: None,
        response: "Hello from the model.".into(),
        routed_provider: None,
        profile: None,
        usage: None,
        provider: "lmstudio".into(),
        model: "test-model-a".into(),
    }));
    let s = h.turns(2);
    assert!(s.contains("Speak"), "Speak offered on a reply:\n{s}");
    h.click(" Speak ");
    match h.find_cmd(|c| matches!(c, Cmd::SandboxSpeak { .. })) {
        Some(Cmd::SandboxSpeak { request }) => {
            assert_eq!(request.leaf, "voice/tts");
            assert_eq!(
                request.run_id,
                "session_memory_gateway_console_sandbox_default_admin"
            );
            assert_eq!(request.body["text"], "Hello from the model.");
            assert_eq!(request.body["provider"], "supertonic");
            assert_eq!(request.body["voice"], "M3");
        }
        other => panic!("expected SandboxSpeak, got {other:?}"),
    }
    let s = h.turns(1);
    assert!(s.contains("Speaking…"), "in-flight label:\n{s}");
}

/// MTP: the picked pair is probed; only reported depths may be sent, an
/// unreported one is refused with the gateway's reason.
#[test]
fn mtp_depths_follow_the_capabilities_probe() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    text_ready(&mut h);
    h.ui.sb_prompt.set("q".into());
    h.review();
    match h.find_cmd(|c| matches!(c, Cmd::SandboxMtpCaps { .. })) {
        Some(Cmd::SandboxMtpCaps { provider, model }) => {
            assert_eq!(
                (provider.as_str(), model.as_str()),
                ("lmstudio", "test-model-a")
            );
        }
        other => panic!("expected SandboxMtpCaps, got {other:?}"),
    }
    use abstractgateway_console::ui::sandbox::{mtp_support_from, publish_mtp};
    publish_mtp(
        &h.store,
        mtp_support_from(
            "lmstudio/test-model-a",
            &json!({"execution": {"speculation": {"supported": true, "supported_depths": [2],
                "message": "depth 2 only on this host"}}}),
        ),
    );
    // A late answer for ANOTHER pair never lands.
    publish_mtp(&h.store, mtp_support_from("other/x", &json!({})));
    assert_eq!(h.store.sandbox_ws.mtp.get_untracked().depths(), &[2]);
    h.store.sandbox_ws.mtp_ix.set(3); // depth 3 — not reported
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    // R15: refusals sit inline under the action bar (adversary note c).
    let notice = abstractgateway_console::ui::sandbox::refusal_now().unwrap_or_default();
    assert!(
        notice.contains("MTP depth 3 is not available")
            && notice.contains("depth 2 only on this host"),
        "{notice}"
    );
    assert!(h
        .find_cmd(|c| matches!(c, Cmd::SandboxTest { .. }))
        .is_none());
    h.store.sandbox_ws.mtp_ix.set(2);
    h.turns(2);
    h.type_text("\r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::SandboxTest { .. })) {
        Some(Cmd::SandboxTest { request, .. }) => {
            assert_eq!(request.0["speculation"]["num_draft_tokens"], 2);
        }
        other => panic!("expected SandboxTest, got {other:?}"),
    }
    // R15: the MTP control is inline; its availability line renders under it.
    h.store.sandbox.set(Loadable::NotAsked);
    let s = h.turns(3);
    assert!(
        s.contains("MTP: depths 2 available"),
        "the inline MTP line:\n{s}"
    );
}
