//! Round 16 (W5's served hint, assigned to the console-TUI lane): the Multimodal page shows the
//! `input.voice` row's `route_hint.sentence` under the Transcription line, byte for byte with no
//! client-side suffix (console.py `routeHintMarkup`, fixture `r15_web_wording_route_hint.json`);
//! only on a configured row; text only (a click sends nothing).

#[path = "round2_shots_workflows.rs"]
#[allow(dead_code)]
mod shots;

use abstractgateway_console::ui::routes::transcription_hint;
use serde_json::{json, Value};
use shots::{routes_screen, voice_routes};

const SENTENCE: &str = "This Mac can transcribe on its GPU with mlx-whisper (large-v3), about ten times faster than faster-whisper on the processor: Apply recommended (Replace mine too) switches it.";

fn hinted(route: bool, configured: bool) -> Value {
    json!({"key": "input.voice", "kind": "input", "modality": "voice", "label": "Voice Input",
           "provider": "faster-whisper", "model": "large-v3", "source": "abstractcore.gateway_runtime",
           "configured": configured,
           "route_hint": {"code": "stt_gpu_available", "sentence": SENTENCE,
                          "route": if route { json!({"provider": "mlx-whisper", "model": "large-v3"}) } else { Value::Null }}})
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_route_hint.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

#[test]
fn the_web_paints_the_sentence_alone() {
    let f = fixture();
    assert_eq!(f["painted"], json!("route_hint.sentence"));
    assert_eq!(f["client_suffix"], Value::Null);
    assert_eq!(f["configured_rows_only"], json!(true));
}

#[test]
fn the_hint_is_the_served_sentence_plus_the_webs_suffix_on_a_configured_row_only() {
    assert_eq!(
        transcription_hint(&voice_routes(hinted(true, true)).rows).as_deref(),
        Some(SENTENCE)
    );
    assert_eq!(
        transcription_hint(&voice_routes(hinted(false, true)).rows).as_deref(),
        Some(SENTENCE)
    );
    assert_eq!(
        transcription_hint(&voice_routes(hinted(true, false)).rows),
        None
    );
    let no_hint = json!({"key": "input.voice", "kind": "input", "modality": "voice", "label": "Voice Input",
                         "provider": "mlx-whisper", "model": "large-v3", "configured": true});
    assert_eq!(transcription_hint(&voice_routes(no_hint).rows), None);
}

#[test]
fn the_multimodal_page_shows_it_under_the_transcription_line_and_a_click_does_nothing() {
    let mut h = routes_screen((160, 48), hinted(true, true));
    let s = h.turns(3);
    let lines: Vec<&str> = s.lines().collect();
    let t = lines
        .iter()
        .position(|l| l.contains("Transcription (speech → text): faster-whisper · large-v3"))
        .unwrap_or_else(|| panic!("{s}"));
    let below = lines[t + 1..(t + 4).min(lines.len())].join(" ");
    assert!(
        below.contains("This Mac can transcribe on its GPU with mlx-whisper"),
        "under the line:\n{s}"
    );
    let below_flat = lines[t + 1..(t + 4).min(lines.len())]
        .iter()
        .map(|l| l.split('│').next_back().unwrap_or("").trim())
        .collect::<Vec<_>>()
        .join(" ");
    assert!(
        below_flat.contains("(Replace mine too) switches it."),
        "the served sentence, whole:\n{below_flat}"
    );
    assert_eq!(
        below_flat.matches("Apply recommended").count(),
        1,
        "no client-side suffix: {below_flat}"
    );
    h.sent();
    let (y, line) = lines
        .iter()
        .enumerate()
        .find(|(_, l)| l.contains("This Mac can transcribe"))
        .unwrap();
    let x = line[..line.find("This Mac").unwrap()].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.turns(2);
    assert!(h.sent().is_empty(), "the hint is text, never a control");
}
