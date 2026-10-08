//! R15 (DESIGN-TUI.md §3.12, §6.1): the Sandbox by mouse. Every Output
//! segment, every action-bar button (Send, Attach, Speak, Play audio,
//! Stop, Clear chat), the discovery Retry buttons and the Attach dialog's
//! buttons get a synthesized SGR click through the real input pipeline,
//! asserted on the Cmd sent, the dialog opened or the state changed. The
//! meta-test enumerates `sandbox::bar_actions` over the fixture states:
//! a bar action without a click test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_sandbox.json`).

mod r8w4;

use std::collections::BTreeSet;
use std::path::PathBuf;

use abstractgateway_console::store::{Loadable, ProvidersData, RoutesData, SandboxOutcome};
use abstractgateway_console::ui::sandbox::{
    self, bar_actions, ArtifactRef, Attachment, BarState, MediaOutcome, SbMode,
};
use abstractgateway_console::ui::{self, review};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    review::view(cx, ctx, &t)
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
             "source": "not_configured", "configured": false},
            {"key": "output.sound", "kind": "output", "modality": "sound", "label": "Sound Output",
             "provider": "acestep", "model": "v1", "configured": true}
        ]
    }))
}

fn providers() -> ProvidersData {
    ProvidersData::from_value(&json!({
        "items": [{"name": "lmstudio", "display_name": "LMStudio", "status": "available",
                   "local_provider": true, "authentication_required": false, "models": []}]
    }))
}

fn reply() -> SandboxOutcome {
    SandboxOutcome {
        ok: true,
        error: None,
        response: "Hello from the model.".into(),
        routed_provider: None,
        profile: None,
        usage: None,
        provider: "lmstudio".into(),
        model: "test-model-a".into(),
    }
}

fn voice_result() -> MediaOutcome {
    MediaOutcome {
        mode: SbMode::Voice,
        provider: "supertonic".into(),
        model: "supertonic-3".into(),
        run_id: "r".into(),
        artifact: ArtifactRef {
            id: "a1".into(),
            content_type: "audio/wav".into(),
            filename: None,
        },
        // A path that does not exist: Play says it cannot, nothing sounds.
        saved: Ok((PathBuf::from("/nonexistent/r15-sandbox-test.wav"), 90_000)),
        image: None,
        image_note: None,
        elapsed_ms: 900,
    }
}

fn page() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    h.store.routes.set(Loadable::Ready(routes()));
    h.store.providers.set(Loadable::Ready(providers()));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into()]),
        );
    });
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-a".into());
    h.ui.sb_prompt.set("hello".into());
    h.turns(3);
    h.sent();
    h
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_sandbox.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn notice(h: &r8w4::Harness) -> String {
    h.store.notice.get_untracked().unwrap_or_default()
}

/// Click the first occurrence of `text` on screen.
fn click(h: &mut r8w4::Harness, text: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(text))
        .unwrap_or_else(|| panic!("{text:?} not on screen:\n{screen}"));
    let b = line.find(text).unwrap();
    let x = line[..b].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn states() -> Vec<BarState> {
    let base = BarState {
        mode: SbMode::Text,
        running: false,
        attachments: 0,
        speakable: false,
        speaking: false,
        audio: false,
    };
    vec![
        base.clone(),
        BarState {
            speakable: true,
            ..base.clone()
        },
        BarState {
            mode: SbMode::Voice,
            audio: true,
            ..base
        },
    ]
}

fn covered() -> BTreeSet<&'static str> {
    ["send", "attach", "speak", "play", "stop", "clear"]
        .into_iter()
        .collect()
}

#[test]
fn every_bar_action_has_a_click_test() {
    let mut offered = BTreeSet::new();
    for s in states() {
        for a in bar_actions(&s) {
            offered.insert(a.id);
        }
    }
    let missing: Vec<_> = offered.difference(&covered()).copied().collect();
    assert!(
        missing.is_empty(),
        "bar actions without a click test: {missing:?}"
    );
    // The Attach dialog's actions are covered by the_attach_dialog_by_mouse.
    let attach: BTreeSet<&str> = sandbox::attach_actions(false)
        .iter()
        .map(|a| a.id)
        .collect();
    assert_eq!(attach, ["remove_all", "upload"].into_iter().collect());
}

#[test]
fn clicking_each_output_segment_picks_its_mode() {
    let mut h = page();
    for m in [
        SbMode::Image,
        SbMode::Voice,
        SbMode::Music,
        SbMode::Sound,
        SbMode::Video,
        SbMode::Text,
    ] {
        let s = h.turns(1);
        let y = s
            .lines()
            .position(|l| l.contains("Output "))
            .expect("Output row");
        let line = s.lines().nth(y).unwrap();
        let b = line.find(&format!(" {} ", m.label())).unwrap() + 1;
        let x = line[..b].chars().count() + 1;
        h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
        assert_eq!(h.store.sandbox_ws.mode.get_untracked(), m, "{}", m.label());
    }
}

#[test]
fn send_by_mouse_runs_the_text_test() {
    let mut h = page();
    click(&mut h, " Send ");
    let cmds = h.sent();
    let body = cmds
        .iter()
        .find_map(|c| match c {
            Cmd::SandboxTest { request, .. } => Some(request.0.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("Send sends SandboxTest: {cmds:?}"));
    assert_eq!(body["prompt"], "hello");
    // While it runs, Send is refused with its reason (no second paid call).
    click(&mut h, " Send ");
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SandboxTest { .. })));
    assert_eq!(notice(&h), sandbox::RUNNING_REASON);
}

#[test]
fn clear_chat_by_mouse_forgets_the_conversation() {
    let mut h = page();
    h.store.sandbox.set(Loadable::Ready(reply()));
    h.store
        .sandbox_ws
        .history
        .set(vec![("q".into(), "a".into())]);
    let s = h.turns(2);
    assert!(s.contains("Hello from the model."), "{s}");
    click(&mut h, " Clear chat ");
    assert!(h.store.sandbox_ws.history.get_untracked().is_empty());
    assert!(matches!(
        h.store.sandbox.get_untracked(),
        Loadable::NotAsked
    ));
    assert!(
        ui::w::notify::toasts().contains(&"sandbox chat cleared".to_string()),
        "a success toast"
    );
}

#[test]
fn speak_play_and_stop_by_mouse() {
    let mut h = page();
    h.store.sandbox.set(Loadable::Ready(reply()));
    h.turns(2);
    click(&mut h, " Speak ");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::SandboxSpeak { request } if request.body["text"] == "Hello from the model.")),
        "Speak sends the reply through voice/tts"
    );
    // An audio result in Voice mode: Play audio and Stop.
    let mut h = page();
    h.store.sandbox_ws.mode.set(SbMode::Voice);
    h.store
        .sandbox_ws
        .media
        .set(Loadable::Ready(voice_result()));
    let s = h.turns(3);
    assert!(s.contains(" Play audio "), "{s}");
    h.store.notice.set(None);
    click(&mut h, " Play audio ");
    assert!(!notice(&h).is_empty(), "Play answers in the status bar");
    h.store.notice.set(None);
    click(&mut h, " Stop ");
    let n = notice(&h);
    assert!(
        n == "nothing is playing" || n == "■ playback stopped",
        "Stop answers: {n}"
    );
}

#[test]
fn the_attach_dialog_by_mouse_survives_a_reload() {
    let mut h = page();
    let s = click(&mut h, " Attach ");
    assert!(s.contains(sandbox::ATTACH_TITLE), "{s}");
    h.type_text("~/fox.png");
    click(&mut h, " Upload ");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::SandboxAttach { path, .. } if path == "~/fox.png")),
        "Upload sends the path"
    );
    // The worker's answer + a routes reload: the dialog stays.
    sandbox::publish_upload(
        &h.store,
        Ok(Attachment {
            name: "fox.png".into(),
            size: 2048,
            content_type: "image/png".into(),
            artifact: json!({"$artifact": "att_9"}),
        }),
    );
    h.store.routes.set(Loadable::Ready(routes()));
    let s = h.turns(3);
    assert!(s.contains(sandbox::ATTACH_TITLE), "survived:\n{s}");
    assert!(s.contains("✓ attached fox.png"), "{s}");
    click(&mut h, " Remove all ");
    assert!(h.store.sandbox_ws.attachments.get_untracked().is_empty());
    let s = click(&mut h, " Close ");
    assert!(!s.contains(sandbox::ATTACH_TITLE), "closed:\n{s}");
}

#[test]
fn the_retry_buttons_by_mouse() {
    use abstractgateway_console::api::{ApiError, ApiErrorKind};
    let mut h = page();
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Failed(ApiError::new(ApiErrorKind::Unreachable, "down")),
        );
    });
    h.turns(2);
    click(&mut h, " Retry model discovery ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadModels { provider } if provider == "lmstudio")));
    h.store.providers.set(Loadable::Failed(ApiError::new(
        ApiErrorKind::Unreachable,
        "down",
    )));
    h.turns(2);
    click(&mut h, " Retry provider discovery ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::LoadProviders)));
}

#[test]
fn hovering_a_segment_shows_the_webs_route_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.contains("Output ")
                .then(|| l.find(" Image ").map(|c| (i, l[..c].chars().count() + 2)))
                .flatten()
        })
        .expect("the Image segment");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Image: mlx-gen / test-flux"), "{s}");
}

#[test]
fn keyboard_tab_reaches_a_segment_and_the_bar_keys_work() {
    let mut h = page();
    // Tab: the first stop is the first Output segment (Text); `a` (outside
    // the message field) opens Attach.
    h.key(b"\t");
    let s = h.key(b"a");
    assert!(s.contains(sandbox::ATTACH_TITLE), "{s}");
    h.esc();
    // Keyboard only: Tab, Tab → the Image segment, Enter picks it.
    let mut h = page();
    h.key(b"\t");
    h.key(b"\t");
    let s = h.key(b"\r");
    assert_eq!(h.store.sandbox_ws.mode.get_untracked(), SbMode::Image);
    assert!(
        s.contains(sandbox::SANDBOX_CONTEXT),
        "the context follows the mode:\n{s}"
    );
}

#[test]
fn the_sandbox_words_are_the_webs() {
    let fx = fixture();
    let s = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(sandbox::OUTPUT_LABEL, s("output_label"));
    assert_eq!(sandbox::SANDBOX_CONTEXT, s("context"));
    assert_eq!(sandbox::SYSTEM_LABEL, s("system_label"));
    assert_eq!(sandbox::SYSTEM_PLACEHOLDER, s("system_placeholder"));
    assert_eq!(sandbox::SYSTEM_HELP, s("system_help"));
    assert_eq!(sandbox::REASONING_LABEL, s("reasoning_label"));
    assert_eq!(sandbox::REASONING_HELP, s("reasoning_help"));
    assert_eq!(sandbox::MTP_LABEL, s("mtp_label"));
    assert_eq!(sandbox::MTP_HELP, s("mtp_help"));
    assert_eq!(sandbox::SEND_LABEL, s("send_label"));
    assert_eq!(sandbox::CLEAR_TIP, s("clear_tip"));
    assert_eq!(sandbox::ATTACH_TIP, s("attach_tip"));
    assert_eq!(sandbox::SECONDS_LABEL, s("seconds_label"));
    assert_eq!(sandbox::SECONDS_HELP, s("seconds_help"));
    assert_eq!(sandbox::SECONDS_REFUSAL, s("seconds_refusal"));
    let list = |k: &str| -> Vec<String> {
        fx[k]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect()
    };
    assert_eq!(
        sandbox::REASONING_CHOICES.to_vec(),
        list("reasoning_options")
    );
    assert_eq!(sandbox::MTP_CHOICES.to_vec(), list("mtp_options"));
    // The bar's tooltips: Attach / Clear are the kit's.
    let st = &states()[0];
    let acts = bar_actions(st);
    let tip = |id: &str| {
        acts.iter()
            .find(|a| a.id == id)
            .unwrap()
            .tooltip
            .clone()
            .unwrap()
    };
    assert_eq!(tip("attach"), s("attach_tip"));
    assert_eq!(tip("clear"), s("clear_tip"));
    // The mode tooltips: "<label>: not configured" for a route without a model.
    let tips = sandbox::mode_tips(Some(&routes().rows));
    assert_eq!(tips[3], format!("Music: {}", s("mode_unconfigured")));
    assert_eq!(tips[2], "Voice: supertonic / supertonic-3");
}

#[test]
fn length_seconds_is_shown_for_sfx_and_rides_the_music_body() {
    let mut h = page();
    // Not on Text.
    let s = h.turns(2);
    assert!(!s.contains(sandbox::SECONDS_LABEL), "{s}");
    h.store.sandbox_ws.mode.set(SbMode::Sound);
    let s = h.turns(3);
    assert!(
        s.contains(sandbox::SECONDS_LABEL),
        "the SFX length field:\n{s}"
    );
    // Click into the field, replace 5 with 2.5, Send by mouse.
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(sandbox::SECONDS_LABEL))
        .unwrap();
    let x = line[..line.find(sandbox::SECONDS_LABEL).unwrap()]
        .chars()
        .count()
        + 21;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.key(b"\x1b[F\x7f2.5");
    assert_eq!(h.store.sandbox_ws.seconds_sound.get_untracked(), "2.5");
    click(&mut h, " Send ");
    let body = h
        .sent()
        .into_iter()
        .find_map(|c| match c {
            Cmd::SandboxMedia { request } => Some(request.body),
            _ => None,
        })
        .expect("SFX sends music/generate");
    assert_eq!(body["seconds"], 2.5);
    assert_eq!(body["task"], "text_to_audio");
    // A bad length refuses with the web's sentence and sends nothing.
    let mut h = page();
    h.store.sandbox_ws.mode.set(SbMode::Sound);
    h.store.sandbox_ws.seconds_sound.set("0".into());
    h.turns(3);
    click(&mut h, " Send ");
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SandboxMedia { .. })));
    assert_eq!(notice(&h), sandbox::SECONDS_REFUSAL);
}
