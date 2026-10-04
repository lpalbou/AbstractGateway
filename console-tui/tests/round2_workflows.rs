//! The Multimodal transcription line: the engine and its true reason.
//! (The round-2 Workflows screen tests moved to `r7w1_workflows.rs` when
//! the page was rebuilt to the web console's parity, R7.2.)

#[path = "round2_shots_workflows.rs"]
#[allow(dead_code)]
mod shots;

use abstractgateway_console::ui::routes::{transcription_line, TranscriptionLevel};
use serde_json::json;
use shots::{voice_missing, voice_ok, voice_routes};

#[test]
fn transcription_line_names_the_engine_and_the_true_reason() {
    let ok = voice_routes(voice_ok());
    let (text, level) = transcription_line(&ok.rows).unwrap();
    assert_eq!(level, TranscriptionLevel::Ready);
    assert!(text.contains("faster-whisper · base — ready"), "{text}");

    let missing = voice_routes(voice_missing());
    let (text, level) = transcription_line(&missing.rows).unwrap();
    assert_eq!(level, TranscriptionLevel::Warn);
    assert!(
        text.contains("Engine missing: unknown AbstractVoice engine 'huggingface'"),
        "{text}"
    );

    let unset = voice_routes(
        json!({"key": "input.voice", "kind": "input", "modality": "voice",
        "label": "Voice Input", "source": "not_configured", "configured": false}),
    );
    let (text, level) = transcription_line(&unset.rows).unwrap();
    assert_eq!(level, TranscriptionLevel::Unset);
    assert!(text.contains("not set"), "{text}");
    assert!(!text.contains("Engine missing"), "{text}");

    let none = voice_routes(
        json!({"key": "input.sound", "kind": "input", "modality": "sound",
        "label": "Sound Input", "configured": false}),
    );
    assert!(transcription_line(&none.rows).is_none());
}

#[test]
fn multimodal_screen_shows_the_transcription_line() {
    let mut h = shots::routes_screen((120, 40), voice_missing());
    let s = h.turns(3);
    assert!(s.contains("Multimodal — which provider"), "{s}");
    assert!(
        s.contains("Transcription (speech → text): huggingface"),
        "{s}"
    );
    assert!(s.contains("Engine missing: unknown AbstractVoice"), "{s}");
}
