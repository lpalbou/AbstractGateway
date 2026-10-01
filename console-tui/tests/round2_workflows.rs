//! Round-2 §4 Workflows screen + the Multimodal transcription line:
//! plain names, what it does, source, used by; the per-app defaults
//! (plain labels, neutral states, broken only with its reason, a picker
//! that saves at once); the transcription engine and its true reason.

#[path = "round2_shots_workflows.rs"]
#[allow(dead_code)]
mod shots;

use abstractgateway_console::store::{
    workflows_from_payload, ConnPhase, Identity, Loadable, RuntimeConfigData,
};
use abstractgateway_console::ui::routes::{transcription_line, TranscriptionLevel};
use abstractgateway_console::ui::workflows::{DEFAULTS_NOT_SERVED, PURPOSE};
use abstractgateway_console::worker::Cmd;
use serde_json::json;
use shots::{
    bundles_payload, runtime_config_payload, voice_missing, voice_ok, voice_routes,
    workflows_screen,
};

#[test]
fn bundle_rows_carry_name_description_source_and_interfaces() {
    let d = workflows_from_payload(&bundles_payload());
    let basic = d
        .rows
        .iter()
        .find(|r| r.bundle_id == "basic-agent")
        .unwrap();
    assert_eq!(basic.name, "Basic agent");
    assert!(basic.description.starts_with("A general chat agent"));
    assert_eq!(basic.source_text(), "Shipped with the gateway");
    assert_eq!(basic.version_text(), "0.3.2 +1 older");
    assert_eq!(
        basic.interfaces,
        vec!["abstractcode.agent.v1", "abstractassistant.agent.v1"]
    );
    let dr = d
        .rows
        .iter()
        .find(|r| r.bundle_id == "deep-research")
        .unwrap();
    assert_eq!(dr.source_text(), "Imported");
    let mine = d.rows.iter().find(|r| r.bundle_id == "my-flow").unwrap();
    assert_eq!(mine.source_text(), "Published from AbstractFlow");
    // No `source` on the item: never guessed.
    let mut v = bundles_payload();
    v["items"][2].as_object_mut().unwrap().remove("source");
    let d = workflows_from_payload(&v);
    let dr = d
        .rows
        .iter()
        .find(|r| r.bundle_id == "deep-research")
        .unwrap();
    assert_eq!(dr.source_text(), "—");
}

#[test]
fn workflows_screen_shows_plain_names_and_used_by_labels() {
    let mut h = workflows_screen((120, 40));
    let s = h.turns(3);
    assert!(s.contains(&PURPOSE[..40]), "purpose line:\n{s}");
    assert!(s.contains("Basic agent"), "{s}");
    assert!(s.contains("Deep research"), "{s}");
    assert!(s.contains("Morning digest · D"), "deprecated shown:\n{s}");
    assert!(s.contains("Researches a question"), "what it does:\n{s}");
    assert!(s.contains("Shipped with the gateway"), "source:\n{s}");
    assert!(s.contains("Imported"), "{s}");
    // Used by: the interface table's plain name, never the raw id.
    assert!(s.contains("used by AbstractCode — c"), "{s}");
    assert!(!s.contains("★agent"), "old status marks gone:\n{s}");
    // The Broken block stays.
    assert!(s.contains("Broken workflows"), "{s}");
    assert!(s.contains("needs abstractruntime >= 0.9"), "{s}");
}

#[test]
fn defaults_section_shows_plain_names_and_neutral_states() {
    let mut h = workflows_screen((120, 40));
    let s = h.turns(3);
    assert!(s.contains("Default workflow per app"), "{s}");
    assert!(
        s.contains("When an app asks for 'an agent' without naming a workflow"),
        "{s}"
    );
    for want in [
        "AbstractCode — chat agent",
        "Built in: Basic agent",
        "Assistant",
        "Basic agent 0.3.2",
        "Deep research",
        "Clients choose",
        "AbstractCode — coding agent",
        "Broken",
        "Other workflow types (1)",
    ] {
        assert!(s.contains(want), "{want}:\n{s}");
    }
    // The folded group is hidden until `o`.
    assert!(!s.contains("Batch map-reduce"), "{s}");
    h.key(b"o");
    let s = h.turns(2);
    assert!(s.contains("Batch map-reduce"), "o unfolds:\n{s}");
    // The raw interface id never labels a row.
    assert!(
        !s.lines().any(|l| l.contains("│abstractcode.agent.v1")),
        "{s}"
    );
}

#[test]
fn a_broken_default_says_why_only_when_selected_and_broken() {
    let mut h = workflows_screen((120, 40));
    let s = h.turns(3);
    assert!(!s.contains("no longer installed"), "{s}");
    // Tab to the defaults table, down to the coding agent (row 3).
    h.key(b"\t");
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    let s = h.turns(3);
    assert!(
        s.contains("Broken: coding-agent 0.2.8 is no longer installed"),
        "the broken reason:\n{s}"
    );
}

#[test]
fn defaults_without_plain_names_fail_loudly() {
    let mut h = workflows_screen((120, 40));
    let mut v = runtime_config_payload();
    for (_, row) in v["agents"]["default_workflow"]
        .as_object_mut()
        .unwrap()
        .iter_mut()
    {
        row.as_object_mut().unwrap().remove("label");
        row.as_object_mut().unwrap().remove("state");
    }
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(&v)));
    let s = h.turns(3);
    assert!(s.contains(&DEFAULTS_NOT_SERVED[..50]), "{s}");
    assert!(!s.contains("Clients choose"), "{s}");
}

#[test]
fn picking_a_default_saves_at_once() {
    let mut h = workflows_screen((120, 40));
    h.turns(2);
    h.sent();
    // Tab to the defaults table, down to Deep research (row 4), Enter.
    h.key(b"\t");
    for _ in 0..3 {
        h.key(b"\x1b[B");
    }
    h.key(b"\r");
    let s = h.turns(3);
    assert!(
        s.contains("Deep research — which workflow runs by default?"),
        "the picker:\n{s}"
    );
    assert!(s.contains("Deep research 1.0.0"), "{s}");
    assert!(s.contains("Clients choose"), "{s}");
    h.key(b"\r");
    let sent = h.sent();
    let body = sent
        .iter()
        .find_map(|c| match c {
            Cmd::SaveRuntimeConfig { body, .. } => Some(body.0.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no save sent: {sent:?}"));
    assert_eq!(
        body,
        json!({"agents": {"default_workflow": {"abstractresearch.deep.v1": "deep-research:research"}}})
    );
}

#[test]
fn a_non_admin_is_told_why_the_defaults_are_hidden() {
    let mut h = workflows_screen((120, 40));
    let id = Identity::from_me(&json!({
        "principal": {"user_id": "ana", "tenant_id": "default", "roles": ["user"], "admin": false},
        "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
    }))
    .unwrap();
    h.store.conn.set(ConnPhase::Connected(id));
    let s = h.turns(3);
    assert!(s.contains("the default workflow per app"), "{s}");
    assert!(!s.contains("Built in: Basic agent"), "{s}");
}

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
