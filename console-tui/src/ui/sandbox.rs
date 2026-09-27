//! The Review screen's SANDBOX workspace — every output mode the web
//! console's Sandbox tab offers (console.py `runSandbox`,
//! `sandboxCandidateRows`, `sandboxRouteMode`):
//!
//! | mode  | route key                      | gateway route                    |
//! |-------|--------------------------------|----------------------------------|
//! | Text  | input.text (pickers here)      | POST /sandbox/generate           |
//! | Image | output.image.text_to_image     | POST /runs/{run}/images/generate |
//! | Voice | output.voice                   | POST /runs/{run}/voice/tts       |
//! | Music | output.music                   | POST /runs/{run}/music/generate  |
//! | SFX   | output.sound                   | POST /runs/{run}/music/generate  |
//! | Video | output.video.text_to_video     | POST /runs/{run}/videos/generate |
//!
//! Media modes use the CONFIGURED capability route's provider/model, as
//! the web does (its pickers are hidden for them); an unconfigured mode
//! refuses with the web's own reason. Text keeps this console's
//! provider/model pickers (the Providers `t` prefill lands there) and
//! gains the web's controls: system prompt, reasoning effort, per-request
//! MTP, and the multi-turn `messages` history (Clear chat resets it).
//!
//! Outputs: a generated image renders INLINE (the artifact preview's
//! bitmap path) and every artifact is SAVED to disk with its path and
//! size shown; audio plays through a local player when one is on PATH
//! (the web plays it in an `<audio>` element). Video has no terminal
//! player: the saved path is the affordance, like the web's raw link.
//!
//! Moved out of review.rs (2026-09-27) so the Finish/journal region there
//! and this workspace can evolve independently; review.rs mounts
//! [`workspace`].

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{json, Value};

use super::util::{error_panel_hint, field, line, line_styled, span, span_bold, wrap_text};
use super::Ctx;
use crate::api::{ApiError, ApiErrorKind, ApiResult, GatewayClient};
use crate::store::{ConnPhase, Loadable, RouteRow, SandboxOutcome, Store};
use crate::worker::Cmd;
use abstracttui::prelude::*;
use abstracttui::widgets::{TextArea, TextAreaState};

// ---------------------------------------------------------------------
// Modes (the web's sandboxRouteMode / sandboxCandidateRows)
// ---------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SbMode {
    Text,
    Image,
    Voice,
    Music,
    Sound,
    Video,
}

impl SbMode {
    /// The web's mode-button order (text, image, voice, music, sound,
    /// video) — also this Select's option order, so index == ordinal.
    pub const ALL: [SbMode; 6] = [
        SbMode::Text,
        SbMode::Image,
        SbMode::Voice,
        SbMode::Music,
        SbMode::Sound,
        SbMode::Video,
    ];

    pub fn index(self) -> usize {
        SbMode::ALL.iter().position(|m| *m == self).unwrap_or(0)
    }

    pub fn from_index(ix: usize) -> SbMode {
        SbMode::ALL.get(ix).copied().unwrap_or(SbMode::Text)
    }

    /// The capability route the web reads for this mode
    /// (`sandboxCandidateRows` wanted keys).
    pub fn route_key(self) -> &'static str {
        match self {
            SbMode::Text => "input.text",
            SbMode::Image => "output.image.text_to_image",
            SbMode::Voice => "output.voice",
            SbMode::Music => "output.music",
            SbMode::Sound => "output.sound",
            SbMode::Video => "output.video.text_to_video",
        }
    }

    /// `sandboxRouteShortLabel`.
    pub fn label(self) -> &'static str {
        match self {
            SbMode::Text => "Text",
            SbMode::Image => "Image",
            SbMode::Voice => "Voice",
            SbMode::Music => "Music",
            SbMode::Sound => "SFX",
            SbMode::Video => "Video",
        }
    }

    /// The prompt placeholder per mode (`updateSandboxControls`).
    pub fn placeholder(self) -> &'static str {
        match self {
            SbMode::Text => "what to send — Enter runs the test",
            SbMode::Image => "describe the image you want to generate — Enter runs",
            SbMode::Voice => "type the sentence to synthesize — Enter runs",
            SbMode::Sound => "describe the sound effect you want to generate — Enter runs",
            SbMode::Music => "describe the music you want to generate — Enter runs",
            SbMode::Video => "describe the video you want to generate — Enter runs",
        }
    }

    /// The artifact kind the result carries (drives inline image vs
    /// saved-file-with-player rendering).
    pub fn is_audio(self) -> bool {
        matches!(self, SbMode::Voice | SbMode::Music | SbMode::Sound)
    }
}

/// The web's row for a media mode: the FIRST route row with exactly the
/// mode's key (`sandboxCandidateRows` keeps the first occurrence).
pub fn mode_row(rows: &[RouteRow], mode: SbMode) -> Option<&RouteRow> {
    rows.iter().find(|r| r.key == mode.route_key())
}

/// `defaultRowConfigured`: provider AND model present on the row.
pub fn row_pair(row: &RouteRow) -> Option<(String, String)> {
    let p = row.provider.clone().filter(|p| !p.trim().is_empty())?;
    let m = row.model.clone().filter(|m| !m.trim().is_empty())?;
    Some((p, m))
}

/// The voice selector the web sends for output.voice
/// (`row.options.voice || row.options.profile`).
pub fn row_voice(row: &RouteRow) -> Option<String> {
    let o = row.options.as_ref()?;
    ["voice", "profile"]
        .iter()
        .find_map(|k| o.get(*k).and_then(Value::as_str))
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(str::to_string)
}

// ---------------------------------------------------------------------
// Request bodies (exact web payloads — unit-tested below)
// ---------------------------------------------------------------------

/// Reasoning choices, the web Sandbox's `<select id="sandbox-reasoning">`
/// ("default" sends nothing).
pub const REASONING_CHOICES: [&str; 7] = [
    "default", "none", "minimal", "low", "medium", "high", "xhigh",
];

/// MTP choices, the web's `<select id="sandbox-speculation">`.
pub const MTP_CHOICES: [&str; 6] = ["inherit", "off", "depth 2", "depth 3", "depth 4", "depth 5"];

/// `speculationFromChoice(choice, undefined, true)`: inherit → absent,
/// off → false, depth N → an explicit native-MTP request that REQUIRES
/// acceleration (the web's strict sandbox form: an unsupported depth
/// fails loudly at the gateway instead of silently running without).
pub fn speculation_for(ix: usize) -> Option<Value> {
    match ix {
        0 => None,
        1 => Some(Value::Bool(false)),
        n if n < MTP_CHOICES.len() => Some(json!({
            "mode": "native_mtp",
            "num_draft_tokens": n as u64,
            "require_acceleration": true,
        })),
        _ => None,
    }
}

/// UTC now as `YYYY-MM-DDTHH:MM:SSZ` (the client_context the web sends
/// carries it; std has no calendar, so civil-from-days by hand).
pub fn utc_iso(epoch_secs: u64) -> String {
    let days = (epoch_secs / 86_400) as i64;
    let rem = epoch_secs % 86_400;
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// The text-mode inputs beyond the pair and prompt.
#[derive(Clone, Debug, Default)]
pub struct TextControls {
    pub system_prompt: String,
    pub reasoning_ix: usize,
    pub mtp_ix: usize,
    /// Completed (user, assistant) turns — the web's `state.sandboxMessages`.
    pub history: Vec<(String, String)>,
}

/// The web text payload (`runSandbox`, mode === "text"). No max_tokens:
/// the web sends none, and a 64-token cap truncated real answers.
pub fn text_body(
    provider: &str,
    model: &str,
    prompt: &str,
    c: &TextControls,
    utc_now: &str,
) -> Value {
    let mut messages = Vec::new();
    for (u, a) in &c.history {
        messages.push(json!({"role": "user", "content": u}));
        messages.push(json!({"role": "assistant", "content": a}));
    }
    let system = c.system_prompt.trim();
    let mut body = json!({
        "capability": "input.text",
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "system_prompt": if system.is_empty() { Value::Null } else { Value::String(system.to_string()) },
        "messages": messages,
        "attachments": [],
        "client_context": {"utc_datetime": utc_now, "source": "terminal"},
    });
    if c.reasoning_ix > 0 {
        if let Some(r) = REASONING_CHOICES.get(c.reasoning_ix) {
            body["reasoning"] = Value::String((*r).to_string());
        }
    }
    if let Some(spec) = speculation_for(c.mtp_ix) {
        body["speculation"] = spec;
    }
    body
}

/// (route leaf, body) for a media mode — the web's per-mode endpoint and
/// payload, field for field. `None` for Text (it has its own route).
pub fn media_body(
    mode: SbMode,
    provider: &str,
    model: &str,
    voice: Option<&str>,
    prompt: &str,
    request_id: &str,
) -> Option<(&'static str, Value)> {
    Some(match mode {
        SbMode::Text => return None,
        SbMode::Image => (
            "images/generate",
            json!({"prompt": prompt, "image_provider": provider, "image_model": model, "request_id": request_id}),
        ),
        SbMode::Voice => {
            let mut b = json!({"text": prompt, "provider": provider, "model": model, "request_id": request_id});
            if let Some(v) = voice {
                b["voice"] = Value::String(v.to_string());
            }
            ("voice/tts", b)
        }
        SbMode::Music | SbMode::Sound => (
            "music/generate",
            json!({
                "prompt": prompt,
                "task": if mode == SbMode::Sound { "text_to_audio" } else { "text_to_music" },
                "music_provider": provider, "music_model": model, "request_id": request_id,
            }),
        ),
        SbMode::Video => (
            "videos/generate",
            json!({"prompt": prompt, "video_provider": provider, "video_model": model, "request_id": request_id}),
        ),
    })
}

/// The sandbox session run id — the web's `sandboxRunId` shape, so both
/// consoles share ONE sandbox run on the gateway.
pub fn sandbox_run_id(tenant: &str, user: &str) -> String {
    fn sanitize(s: &str, fallback: &str) -> String {
        let out: String = s
            .to_lowercase()
            .chars()
            .map(|c| {
                if c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, ':' | '-' | '_') {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        let out = out.trim_matches('_').to_string();
        if out.is_empty() {
            fallback.to_string()
        } else {
            out
        }
    }
    format!(
        "session_memory_gateway_console_sandbox_{}_{}",
        sanitize(tenant, "default"),
        sanitize(user, "user")
    )
}

// ---------------------------------------------------------------------
// Response parsing
// ---------------------------------------------------------------------

/// What the text answer carries beyond `SandboxOutcome` (the web's
/// finalizeSandboxMessage meta: reasoning block, usage label, MTP line).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TextMeta {
    pub reasoning: Option<String>,
    pub usage_label: String,
    pub speculation: String,
}

impl TextMeta {
    pub fn from_value(v: &Value, elapsed_ms: u64) -> TextMeta {
        let reasoning = v
            .get("reasoning")
            .and_then(Value::as_str)
            .filter(|r| !r.trim().is_empty())
            .map(str::to_string);
        TextMeta {
            reasoning,
            usage_label: usage_label(v.get("usage"), elapsed_ms),
            speculation: speculation_summary(v.get("speculation")),
        }
    }
}

/// `sandboxUsageLabel`: elapsed · N tok · N tok/s.
pub fn usage_label(usage: Option<&Value>, elapsed_ms: u64) -> String {
    let mut parts = Vec::new();
    if elapsed_ms > 0 {
        parts.push(if elapsed_ms < 1000 {
            format!("{elapsed_ms}ms")
        } else if elapsed_ms < 10_000 {
            format!("{:.1}s", elapsed_ms as f64 / 1000.0)
        } else {
            format!("{:.0}s", elapsed_ms as f64 / 1000.0)
        });
    }
    let tokens = usage
        .and_then(|u| {
            [
                "completion_tokens",
                "output_tokens",
                "generated_tokens",
                "total_tokens",
            ]
            .iter()
            .find_map(|k| u.get(*k).and_then(Value::as_f64))
        })
        .unwrap_or(0.0);
    if tokens > 0.0 {
        parts.push(format!("{} tok", tokens.round() as u64));
        let sec = elapsed_ms as f64 / 1000.0;
        if sec > 0.0 {
            parts.push(format!("{} tok/s", ((tokens / sec).round() as u64).max(1)));
        }
    }
    parts.join(" · ")
}

/// `speculationSummary`.
pub fn speculation_summary(v: Option<&Value>) -> String {
    let Some(v) = v.filter(|v| !v.is_null() && v.as_bool() != Some(false)) else {
        return "MTP execution not reported".into();
    };
    if v.get("used").and_then(Value::as_bool) == Some(true) {
        return match v.get("num_draft_tokens").and_then(Value::as_u64) {
            Some(n) if n > 0 => format!("MTP used (depth {n})"),
            _ => "MTP used".into(),
        };
    }
    let why = v
        .get("message")
        .and_then(Value::as_str)
        .or_else(|| v.get("reason").and_then(Value::as_str))
        .filter(|s| !s.is_empty());
    match why {
        Some(w) => format!("MTP not used: {w}"),
        None => "MTP not used".into(),
    }
}

/// The artifact a media response returned: the web reads
/// `image_artifact || audio_artifact || music_artifact || video_artifact`
/// and the id from `$artifact || artifact_id || id`.
#[derive(Clone, Debug, PartialEq)]
pub struct ArtifactRef {
    pub id: String,
    pub content_type: String,
    pub filename: Option<String>,
}

/// Parse a media route response. `ok:false` (the routes' 200-with-error
/// shape, e.g. `code: generated_image_unavailable`) is an ERROR with the
/// web's message precedence `error || code || "Generation failed."`.
pub fn parse_media_response(v: &Value) -> Result<ArtifactRef, String> {
    if v.get("ok").and_then(Value::as_bool) == Some(false) {
        let msg = ["error", "code"]
            .iter()
            .find_map(|k| v.get(*k).and_then(Value::as_str).filter(|s| !s.is_empty()))
            .unwrap_or("Generation failed.");
        return Err(msg.to_string());
    }
    let r = [
        "image_artifact",
        "audio_artifact",
        "music_artifact",
        "video_artifact",
    ]
    .iter()
    .find_map(|k| v.get(*k).filter(|x| x.is_object()))
    .ok_or_else(|| "the gateway reported success but returned no artifact reference".to_string())?;
    let id = ["$artifact", "artifact_id", "id"]
        .iter()
        .find_map(|k| r.get(*k).and_then(Value::as_str))
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("artifact reference without an id: {r}"))?;
    Ok(ArtifactRef {
        id: id.to_string(),
        content_type: r
            .get("content_type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        filename: r
            .get("filename")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string),
    })
}

/// File extension for a saved artifact: the content type first, then the
/// artifact's own filename, else the mode's route default format (the
/// routes default to png / wav / mp4).
pub fn artifact_ext(r: &ArtifactRef, mode: SbMode) -> String {
    let ct = r.content_type.to_ascii_lowercase();
    let by_ct = match ct.split(';').next().unwrap_or("").trim() {
        "image/png" => Some("png"),
        "image/jpeg" | "image/jpg" => Some("jpg"),
        "image/webp" => Some("webp"),
        "image/gif" => Some("gif"),
        "audio/wav" | "audio/x-wav" | "audio/wave" | "audio/vnd.wave" => Some("wav"),
        "audio/mpeg" | "audio/mp3" => Some("mp3"),
        "audio/flac" | "audio/x-flac" => Some("flac"),
        "audio/ogg" => Some("ogg"),
        "video/mp4" => Some("mp4"),
        "video/webm" => Some("webm"),
        "video/quicktime" => Some("mov"),
        _ => None,
    };
    if let Some(e) = by_ct {
        return e.to_string();
    }
    if let Some(e) = r
        .filename
        .as_deref()
        .and_then(|f| Path::new(f).extension())
        .and_then(|e| e.to_str())
        .filter(|e| !e.is_empty() && e.chars().all(|c| c.is_ascii_alphanumeric()))
    {
        return e.to_ascii_lowercase();
    }
    match mode {
        SbMode::Image => "png",
        SbMode::Voice | SbMode::Music | SbMode::Sound => "wav",
        SbMode::Video => "mp4",
        SbMode::Text => "bin",
    }
    .to_string()
}

/// Where generated artifacts are saved: `~/Downloads/abstractgateway-console`
/// when the Downloads folder exists (where a user looks for a file they
/// just made), else the system temp folder. The full path is always shown.
pub fn artifact_dir() -> PathBuf {
    if let Some(home) = std::env::var_os("HOME") {
        let dl = PathBuf::from(home).join("Downloads");
        if dl.is_dir() {
            return dl.join("abstractgateway-console");
        }
    }
    std::env::temp_dir().join("abstractgateway-console")
}

/// Human byte size (the web shows none; the terminal's saved file needs one).
pub fn human_bytes(n: u64) -> String {
    const U: [&str; 4] = ["B", "KB", "MB", "GB"];
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < U.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", U[i])
    }
}

// ---------------------------------------------------------------------
// Worker-side jobs (called from worker.rs arms)
// ---------------------------------------------------------------------

/// One media generation, fully described UI-side.
#[derive(Clone, Debug)]
pub struct MediaRequest {
    pub mode: SbMode,
    pub provider: String,
    pub model: String,
    pub run_id: String,
    pub leaf: String,
    pub body: Value,
    pub dest_dir: PathBuf,
}

impl MediaRequest {
    pub fn busy_label(&self) -> String {
        format!(
            "sandbox {}: {}/{}",
            self.mode.label().to_lowercase(),
            self.provider,
            self.model
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct MediaOutcome {
    pub mode: SbMode,
    pub provider: String,
    pub model: String,
    pub run_id: String,
    pub artifact: ArtifactRef,
    /// Saved file + its size; Err = the generation succeeded but the
    /// download/write failed (the artifact still exists on the gateway).
    pub saved: Result<(PathBuf, u64), String>,
    /// Decoded image for inline rendering (image mode only).
    pub image: Option<Arc<Bitmap>>,
    /// Why an image could not be shown inline (decode failure).
    pub image_note: Option<String>,
    pub elapsed_ms: u64,
}

/// POST the media route, then save the artifact (and decode an image).
/// A generation failure is an `Err`; a save failure after a successful
/// generation is reported INSIDE the outcome (the artifact is real).
pub fn perform_media(c: &GatewayClient, req: &MediaRequest) -> ApiResult<MediaOutcome> {
    let started = std::time::Instant::now();
    let v = c.sandbox_media(&req.run_id, &req.leaf, &req.body)?;
    let artifact = parse_media_response(&v).map_err(|m| ApiError {
        kind: ApiErrorKind::Protocol,
        message: m,
        body: Some(v.clone()),
        timed_out: false,
    })?;
    let dest = req.dest_dir.join(format!(
        "sandbox-{}-{}.{}",
        req.mode.label().to_lowercase(),
        artifact
            .id
            .chars()
            .map(
                |ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                    ch
                } else {
                    '_'
                }
            )
            .collect::<String>(),
        artifact_ext(&artifact, req.mode)
    ));
    let saved = c
        .save_artifact(&req.run_id, &artifact.id, &dest)
        .map(|n| (dest.clone(), n))
        .map_err(|e| e.message);
    let (mut image, mut image_note) = (None, None);
    if req.mode == SbMode::Image {
        if let Ok((path, _)) = &saved {
            match std::fs::read(path)
                .map_err(|e| e.to_string())
                .and_then(|b| abstracttui::gfx::decode_image(&b).map_err(|e| e.to_string()))
            {
                Ok(bmp) => image = Some(Arc::new(bmp)),
                Err(e) => image_note = Some(format!("cannot decode this image here: {e}")),
            }
        }
    }
    Ok(MediaOutcome {
        mode: req.mode,
        provider: req.provider.clone(),
        model: req.model.clone(),
        run_id: req.run_id.clone(),
        artifact,
        saved,
        image,
        image_note,
        elapsed_ms: started.elapsed().as_millis() as u64,
    })
}

/// Publish a text answer (UI thread): the outcome slot the status line
/// reads + the web's meta (reasoning, usage label, MTP summary).
pub fn publish_text(
    store: &Store,
    provider: &str,
    model: &str,
    result: ApiResult<Value>,
    elapsed_ms: u64,
) {
    match result {
        Ok(v) => {
            store
                .sandbox_ws
                .text_meta
                .set(Some(TextMeta::from_value(&v, elapsed_ms)));
            store
                .sandbox
                .set(Loadable::Ready(SandboxOutcome::from_value(
                    provider, model, &v,
                )));
        }
        Err(e) => {
            store.sandbox_ws.text_meta.set(None);
            store.sandbox.set(Loadable::Failed(e));
        }
    }
}

// ---------------------------------------------------------------------
// State (one Copy bundle of signals, held by the Store)
// ---------------------------------------------------------------------

#[derive(Clone, Copy)]
pub struct SandboxWs {
    pub mode: Signal<SbMode>,
    pub system: Signal<String>,
    pub reasoning_ix: Signal<usize>,
    pub mtp_ix: Signal<usize>,
    /// Completed turns sent as `messages` on the next text call.
    pub history: Signal<Vec<(String, String)>>,
    /// The prompt of the text call whose outcome is in `store.sandbox`
    /// (folded into `history` when the next turn is sent).
    pub last_prompt: Signal<String>,
    pub text_meta: Signal<Option<TextMeta>>,
    pub media: Signal<Loadable<MediaOutcome>>,
}

impl SandboxWs {
    pub fn create(cx: Scope) -> SandboxWs {
        SandboxWs {
            mode: cx.signal(SbMode::Text),
            system: cx.signal(String::new()),
            reasoning_ix: cx.signal(0),
            mtp_ix: cx.signal(0),
            history: cx.signal(Vec::new()),
            last_prompt: cx.signal(String::new()),
            text_meta: cx.signal(None),
            media: cx.signal(Loadable::NotAsked),
        }
    }

    /// Forget the conversation and results (new gateway, or Clear chat).
    /// The controls (mode, system prompt, reasoning, MTP) are the
    /// operator's settings and survive.
    pub fn reset(&self) {
        self.history.set(Vec::new());
        self.last_prompt.set(String::new());
        self.text_meta.set(None);
        self.media.set(Loadable::NotAsked);
    }
}

// ---------------------------------------------------------------------
// Audio playback (a local player, when one exists)
// ---------------------------------------------------------------------

/// The first audio player on PATH: macOS `afplay`, PulseAudio `paplay`,
/// ALSA `aplay`, then `ffplay` (headless flags).
pub fn find_player() -> Option<(PathBuf, Vec<&'static str>)> {
    let path = std::env::var_os("PATH")?;
    for (bin, args) in [
        ("afplay", vec![]),
        ("paplay", vec![]),
        ("aplay", vec!["-q"]),
        ("ffplay", vec!["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ] {
        for dir in std::env::split_paths(&path) {
            let p = dir.join(bin);
            if p.is_file() {
                return Some((p, args));
            }
        }
    }
    None
}

thread_local! {
    static PLAYER: std::cell::RefCell<Option<std::process::Child>> =
        const { std::cell::RefCell::new(None) };
}

fn stop_player() -> bool {
    PLAYER.with(|p| {
        if let Some(mut child) = p.borrow_mut().take() {
            let running = matches!(child.try_wait(), Ok(None));
            let _ = child.kill();
            let _ = child.wait();
            running
        } else {
            false
        }
    })
}

fn play_file(path: &Path) -> Result<String, String> {
    let (bin, args) = find_player().ok_or_else(|| {
        "no audio player on PATH (afplay, paplay, aplay or ffplay) — open the saved file"
            .to_string()
    })?;
    stop_player();
    let child = std::process::Command::new(&bin)
        .args(&args)
        .arg(path)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("cannot start {}: {e}", bin.display()))?;
    PLAYER.with(|p| *p.borrow_mut() = Some(child));
    Ok(format!(
        "▶ playing through {}",
        bin.file_name().and_then(|n| n.to_str()).unwrap_or("player")
    ))
}

// ---------------------------------------------------------------------
// The workspace view
// ---------------------------------------------------------------------

/// The sandbox block (Review screen body). `g` runs from anywhere inside
/// it; Enter in the prompt is the advertised gesture.
pub fn workspace(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let ws = store.sandbox_ws;
    let tt = *t;

    let prov_ix = cx.signal(0usize);
    let model_ix = cx.signal(0usize);
    let mode_ix = cx.signal(ws.mode.get_untracked().index());

    // Provider index ⇄ durable name (see the pre-move review.rs notes:
    // tracked reads, equality guards, a missing saved name clears only
    // against a READY non-empty list).
    cx.effect(move || {
        let names = provider_names_tracked(&store);
        let want = ui.sb_provider.get();
        if want.is_empty() {
            if prov_ix.get_untracked() != 0 {
                prov_ix.set(0);
            }
            return;
        }
        match names.iter().position(|n| *n == want) {
            Some(i) => {
                if prov_ix.get_untracked() != i + 1 {
                    prov_ix.set(i + 1);
                }
            }
            None if !names.is_empty() => {
                prov_ix.set(0);
                ui.sb_provider.set(String::new());
            }
            None => {}
        }
    });
    cx.effect(move || {
        let ix = prov_ix.get();
        if ix == 0 {
            if model_ix.get_untracked() != 0 {
                model_ix.set(0);
            }
            return;
        }
        let names = provider_names_tracked(&store);
        let Some(name) = names.get(ix - 1).cloned() else {
            return;
        };
        let list = store
            .models
            .with(|m| m.get(&name).and_then(|l| l.ready().cloned()));
        let want = ui.sb_model.get();
        if let Some(models) = list.filter(|m| !m.is_empty()) {
            match models.iter().position(|m| *m == want) {
                Some(i) => {
                    if model_ix.get_untracked() != i + 1 {
                        model_ix.set(i + 1);
                    }
                }
                None => {
                    if model_ix.get_untracked() != 0 {
                        model_ix.set(0);
                    }
                    if !want.is_empty() {
                        ui.sb_model.set(String::new());
                    }
                }
            }
        }
    });
    {
        let ctx2 = ctx.clone();
        cx.effect(move || {
            let ix = prov_ix.get();
            if ix == 0 {
                return;
            }
            let names = provider_names_tracked(&store);
            let Some(name) = names.get(ix - 1).cloned() else {
                return;
            };
            let needs = store.models.with_untracked(|m| !m.contains_key(&name));
            if needs {
                store
                    .models
                    .update(|m| drop(m.insert(name.clone(), Loadable::Loading)));
                ctx2.send(Cmd::LoadModels { provider: name });
            }
        });
    }
    // The media modes read the capability routes (the web's
    // `state.defaults`): load them once connected if nobody has.
    {
        let ctx2 = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && store
                    .routes
                    .with_untracked(|r| matches!(r, Loadable::NotAsked))
            {
                store.routes.set(Loadable::Loading);
                ctx2.send(Cmd::LoadRoutes);
            }
        });
    }
    // Mode select ⇄ durable mode (both equality-guarded: one hop).
    cx.effect(move || {
        let m = SbMode::from_index(mode_ix.get());
        if ws.mode.get_untracked() != m {
            ws.mode.set(m);
        }
    });
    cx.effect(move || {
        let ix = ws.mode.get().index();
        if mode_ix.get_untracked() != ix {
            mode_ix.set(ix);
        }
    });

    let prompt_state = TextAreaState::new(cx);
    prompt_state.set_text(ui.sb_prompt.get_untracked());

    let ctx_g = ctx.clone();
    let ps_g = prompt_state.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .shortcut(KeyChord::plain(Key::Char('g')), move |_| {
            run(&ctx_g, prov_ix, model_ix, &ps_g);
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Live test (sandbox generate)")
                .fill(t.surface)
                .layout(LayoutStyle::column().gap(0).grow(1.0).padding(Edges::hv(1, 0)))
                // Mode picker + the mode's teaching line on ONE row (the
                // 80x24 budget: every added control row costs the body).
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), move |gcx| {
                    let t = tt;
                    let rows = store.routes.with(|r| r.ready().map(|d| d.rows.clone()));
                    let opts: Vec<SelectOption> = SbMode::ALL
                        .iter()
                        .map(|m| SelectOption::new(mode_option_label(*m, rows.as_deref())))
                        .collect();
                    let mode = ws.mode.get();
                    let teach = match mode {
                        SbMode::Text => "run a REAL text generation through the gateway to prove a provider/model pair works".to_string(),
                        m => format!("generate {} through the configured {} route", m.label().to_lowercase(), m.route_key()),
                    };
                    Element::new()
                        .style(LayoutStyle::row().gap(1).h(1))
                        .child(
                            Element::new()
                                .style(LayoutStyle::default().w(18 + 1 + 28).h(1).shrink(0.0))
                                .child(field(
                                    &t,
                                    "mode",
                                    Select::new(opts)
                                        .value(mode_ix)
                                        .layout(LayoutStyle::default().w(28).h(1).shrink(0.0))
                                        .element(gcx, &t)
                                        .build(),
                                ))
                                .build(),
                        )
                        .child(line(vec![span(teach, t.text_muted)]))
                        .build()
                }))
                // Text: the pair pickers + controls. Media: the route line.
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
                    let ctx2 = ctx.clone();
                    move |gcx| {
                        let t = tt;
                        match ws.mode.get() {
                            SbMode::Text => text_controls(gcx, &ctx2, &t, prov_ix, model_ix),
                            m => route_line(&t, &store, m),
                        }
                    }
                }))
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
                    let prompt_state = prompt_state.clone();
                    let ctx_submit = ctx.clone();
                    move |gcx| {
                        let t = tt;
                        let ctx3 = ctx_submit.clone();
                        let st = prompt_state.clone();
                        let st2 = prompt_state.clone();
                        field(
                            &t,
                            "prompt",
                            TextArea::new()
                                .state(&st)
                                .placeholder(ws.mode.get().placeholder())
                                .on_change(move |s: &str| {
                                    if ui.sb_prompt.with_untracked(|p| p != s) {
                                        ui.sb_prompt.set(s.to_string());
                                    }
                                })
                                .on_submit(move |_| {
                                    run(&ctx3, prov_ix, model_ix, &st2);
                                })
                                .layout(
                                    LayoutStyle::default()
                                        .basis(Dimension::Cells(0))
                                        .grow(1.0)
                                        .min_h(2)
                                        .max_h(2)
                                        .shrink(0.0),
                                )
                                .element(gcx, &t)
                                .autofocus()
                                .build(),
                        )
                    }
                }))
                .child(dyn_view_scoped(LayoutStyle::default().h(1).shrink(0.0), {
                    let ctx_btn = ctx.clone();
                    let ps_btn = prompt_state.clone();
                    move |gcx| {
                        let t = tt;
                        let ctx3 = ctx_btn.clone();
                        let ctx4 = ctx_btn.clone();
                        let ps = ps_btn.clone();
                        let running = store.sandbox.with(Loadable::is_loading)
                            || ws.media.with(Loadable::is_loading);
                        let mut row = Element::new()
                            .style(LayoutStyle::row().gap(2))
                            .child(
                                Button::new("Generate (Enter)")
                                    .disabled(running)
                                    .on_click(move || run(&ctx3, prov_ix, model_ix, &ps))
                                    .element(gcx, &t)
                                    .build(),
                            )
                            .child(
                                Button::new("Clear")
                                    .disabled(running)
                                    .on_click(move || clear(&ctx4))
                                    .element(gcx, &t)
                                    .build(),
                            );
                        // Text mode: the web's system / reasoning / MTP
                        // controls, on the button row (the 80x24 budget
                        // has no spare row). The system prompt edits in
                        // a small dialog; its label says whether one is set.
                        if ws.mode.get() == SbMode::Text {
                            let ctx5 = ctx_btn.clone();
                            let sys_label = if ws.system.with(|s| s.trim().is_empty()) {
                                "System…"
                            } else {
                                "System ✓"
                            };
                            let reasoning_opts: Vec<SelectOption> =
                                REASONING_CHOICES.iter().map(|r| SelectOption::new(*r)).collect();
                            let mtp_opts: Vec<SelectOption> =
                                MTP_CHOICES.iter().map(|r| SelectOption::new(*r)).collect();
                            row = row
                                .child(
                                    Button::new(sys_label)
                                        .on_click(move || open_system_prompt(&ctx5, gcx))
                                        .element(gcx, &t)
                                        .build(),
                                )
                                .child(
                                    Element::new()
                                        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                                        .child(line_styled(
                                            LayoutStyle::default().w(6).h(1).shrink(0.0),
                                            vec![span("reason", t.text_muted)],
                                        ))
                                        .child(
                                            Select::new(reasoning_opts)
                                                .value(ws.reasoning_ix)
                                                .layout(LayoutStyle::default().w(11).h(1).shrink(0.0))
                                                .element(gcx, &t)
                                                .build(),
                                        )
                                        .child(line_styled(
                                            LayoutStyle::default().w(3).h(1).shrink(0.0),
                                            vec![span("MTP", t.text_muted)],
                                        ))
                                        .child(
                                            Select::new(mtp_opts)
                                                .value(ws.mtp_ix)
                                                .layout(LayoutStyle::default().w(11).h(1).shrink(0.0))
                                                .element(gcx, &t)
                                                .build(),
                                        )
                                        .build(),
                                );
                        }
                        // Play/Stop for a saved AUDIO result (the web's
                        // <audio controls>), shown only in its mode.
                        if let Loadable::Ready(o) = ws.media.get() {
                            if o.mode.is_audio() && o.mode == ws.mode.get() {
                                if let Ok((path, _)) = &o.saved {
                                    let path = path.clone();
                                    row = row
                                        .child(
                                            Button::new("Play audio")
                                                .on_click(move || {
                                                    let msg = play_file(&path).unwrap_or_else(|e| e);
                                                    store.notice.set(Some(msg));
                                                })
                                                .element(gcx, &t)
                                                .build(),
                                        )
                                        .child(
                                            Button::new("Stop")
                                                .on_click(move || {
                                                    let was = stop_player();
                                                    store.notice.set(Some(
                                                        if was { "■ playback stopped" } else { "nothing is playing" }.into(),
                                                    ));
                                                })
                                                .element(gcx, &t)
                                                .build(),
                                        );
                                }
                            }
                        }
                        row.build()
                    }
                }))
                .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
                    match ws.mode.get() {
                        SbMode::Text => text_status(
                            &tt,
                            &store.sandbox.get(),
                            ws.text_meta.get().as_ref(),
                            ws.history.with(Vec::len),
                        ),
                        m => media_status(&tt, m, &ws.media.get()),
                    }
                }))
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
                    match ws.mode.get() {
                        SbMode::Text => text_body_view(gcx, &tt, &store),
                        m => media_body_view(gcx, &tt, m, &ws.media.get()),
                    }
                }))
                .element(t)
                .build(),
        )
        .build()
}

fn mode_option_label(m: SbMode, rows: Option<&[RouteRow]>) -> String {
    if m == SbMode::Text {
        return "Text chat".into();
    }
    match rows {
        None => format!("{} — routes not loaded", m.label()),
        Some(rows) => match mode_row(rows, m) {
            None => format!("{} — not offered", m.label()),
            Some(r) => match row_pair(r) {
                Some((_, model)) => format!("{} — {model}", m.label()),
                None => format!("{} — not configured", m.label()),
            },
        },
    }
}

fn route_line(t: &TokenSet, store: &Store, m: SbMode) -> View {
    let msg = match store.routes.get() {
        Loadable::NotAsked => (
            "routes not loaded yet — connect first".to_string(),
            t.text_muted,
        ),
        Loadable::Loading => ("⟳ reading capability routes…".into(), t.info),
        Loadable::Failed(e) => (
            format!("✗ capability routes unavailable: {}", e.message),
            t.error,
        ),
        Loadable::Ready(d) => match mode_row(&d.rows, m) {
            None => (
                format!(
                    "{} ({}) is not offered by this gateway",
                    m.label(),
                    m.route_key()
                ),
                t.warn,
            ),
            Some(r) => match row_pair(r) {
                Some((p, model)) => {
                    let voice = if m == SbMode::Voice {
                        row_voice(r)
                            .map(|v| format!(" · voice {v}"))
                            .unwrap_or_default()
                    } else {
                        String::new()
                    };
                    (
                        format!("{} will use {p} / {model}{voice}", m.route_key()),
                        t.text,
                    )
                }
                None => (
                    format!(
                        "{} is not configured yet — configure it on Routes (3) first",
                        m.route_key()
                    ),
                    t.warn,
                ),
            },
        },
    };
    field(t, "route", line(vec![span(msg.0, msg.1)]))
}

/// Text mode: provider + model pickers (unchanged honesty arms) and the
/// web's system / reasoning / MTP controls on one row.
fn text_controls(
    gcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    prov_ix: Signal<usize>,
    model_ix: Signal<usize>,
) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let t = *t;
    let provider_row = match store.providers.get() {
        Loadable::NotAsked => field(
            &t,
            "provider",
            line(vec![span(
                "— not loaded yet (connect first, or press r to refresh)",
                t.text_muted,
            )]),
        ),
        Loadable::Loading => field(
            &t,
            "provider",
            line(vec![span("⟳ discovering providers…", t.info)]),
        ),
        Loadable::Failed(e) => {
            let ctx3 = ctx.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(field(
                    &t,
                    "provider",
                    line(vec![span(
                        format!("✗ discovery failed: {}", e.message),
                        t.error,
                    )]),
                ))
                .child(field(
                    &t,
                    "",
                    Button::new("Retry provider discovery")
                        .on_click(move || {
                            ctx3.store.providers.set(Loadable::Loading);
                            ctx3.send(Cmd::LoadProviders);
                        })
                        .element(gcx, &t)
                        .build(),
                ))
                .build()
        }
        Loadable::Ready(d) if d.items.is_empty() => field(
            &t,
            "provider",
            line(vec![span(
                "∅ no providers discovered — add one on the Providers screen (2)",
                t.text_muted,
            )]),
        ),
        Loadable::Ready(d) => {
            let names: Vec<String> = d.items.iter().map(|i| i.name.clone()).collect();
            let opts: Vec<SelectOption> = std::iter::once(SelectOption::new("choose a provider…"))
                .chain(names.iter().map(|n| SelectOption::new(n.clone())))
                .collect();
            field(
                &t,
                "provider",
                Select::new(opts)
                    .value(prov_ix)
                    .on_change(move |ix: usize| {
                        let name = if ix == 0 || ix > names.len() {
                            String::new()
                        } else {
                            names[ix - 1].clone()
                        };
                        if ui.sb_provider.get_untracked() != name {
                            ui.sb_provider.set(name);
                            ui.sb_model.set(String::new());
                            ui.sb_model_custom.set(String::new());
                            model_ix.set(0);
                        }
                    })
                    .layout(LayoutStyle::default().w(40).h(1).shrink(0.0))
                    .element(gcx, &t)
                    .build(),
            )
        }
    };

    let model_row = {
        let ix = prov_ix.get();
        let names = provider_names_tracked(&store);
        match names.get(ix.wrapping_sub(1)).cloned().filter(|_| ix > 0) {
            None => field(
                &t,
                "model",
                line(vec![span("choose a provider first", t.text_faint)]),
            ),
            Some(name) => {
                let entry = store
                    .models
                    .with(|m| m.get(&name).cloned())
                    .unwrap_or(Loadable::NotAsked);
                match entry {
                    Loadable::Ready(models) if !models.is_empty() => {
                        let opts: Vec<SelectOption> =
                            std::iter::once(SelectOption::new("choose a model…"))
                                .chain(models.iter().map(|m| SelectOption::new(m.clone())))
                                .collect();
                        let models2 = models.clone();
                        field(
                            &t,
                            "model",
                            Combobox::new(opts)
                                .value(model_ix)
                                .placeholder("type to filter…")
                                .on_change(move |mix: usize| {
                                    let m = if mix == 0 || mix > models2.len() {
                                        String::new()
                                    } else {
                                        models2[mix - 1].clone()
                                    };
                                    if ui.sb_model.get_untracked() != m {
                                        ui.sb_model.set(m);
                                    }
                                })
                                .layout(LayoutStyle::default().w(52).h(1).shrink(0.0))
                                .element(gcx, &t)
                                .build(),
                        )
                    }
                    Loadable::Loading | Loadable::NotAsked => field(
                        &t,
                        "model",
                        line(vec![span("⟳ discovering models…", t.info)]),
                    ),
                    Loadable::Failed(e) => {
                        let ctx3 = ctx.clone();
                        let name_btn = name.clone();
                        Element::new()
                            .style(LayoutStyle::column().gap(0))
                            .child(field(
                                &t,
                                "model",
                                TextInput::new()
                                    .value(ui.sb_model_custom)
                                    .placeholder("discovery failed — type the model id")
                                    .placeholder_while_focused(true)
                                    .layout(LayoutStyle::default().w(52).h(1))
                                    .element(gcx, &t)
                                    .build(),
                            ))
                            .child(field(
                                &t,
                                "",
                                line(vec![span(
                                    format!("discovery failed: {}", e.message),
                                    t.error,
                                )]),
                            ))
                            .child(field(
                                &t,
                                "",
                                Button::new("Retry model discovery")
                                    .on_click(move || {
                                        let n = name_btn.clone();
                                        ctx3.store.models.update(|m| {
                                            drop(m.insert(n.clone(), Loadable::Loading))
                                        });
                                        ctx3.send(Cmd::LoadModels { provider: n });
                                    })
                                    .element(gcx, &t)
                                    .build(),
                            ))
                            .build()
                    }
                    Loadable::Ready(_) => field(
                        &t,
                        "model",
                        TextInput::new()
                            .value(ui.sb_model_custom)
                            .placeholder("no discoverable models — type the model id")
                            .placeholder_while_focused(true)
                            .layout(LayoutStyle::default().w(52).h(1))
                            .element(gcx, &t)
                            .build(),
                    ),
                }
            }
        }
    };

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(provider_row)
        .child(model_row)
        .build()
}

/// The system-prompt dialog (the web's inline "System prompt" input).
fn open_system_prompt(ctx: &Ctx, cx: Scope) {
    let ws = ctx.store.sandbox_ws;
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    let size = Size::new(vp.w.clamp(1, 90), 9);
    super::open_form(ctx, cx, size, move |mcx, close| {
        let t0 = use_theme(mcx).get().tokens;
        let c1 = close.clone();
        let c2 = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Sandbox system prompt", t0.accent)]))
            .child(line(vec![span(
                "sent with every text turn; empty = the gateway's default test-assistant prompt",
                t0.text_faint,
            )]))
            .child(
                TextInput::new()
                    .value(ws.system)
                    .placeholder("optional")
                    .on_submit(move |_| c1())
                    .layout(LayoutStyle::default().grow(1.0).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            )
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Done (Enter/Esc)")
                            .on_click(move || c2())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Clear")
                            .on_click(move || ws.system.set(String::new()))
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

/// Tracked provider-name read (the picker's reactive source).
fn provider_names_tracked(store: &Store) -> Vec<String> {
    store.providers.with(|p| {
        p.ready()
            .map(|d| d.items.iter().map(|i| i.name.clone()).collect())
            .unwrap_or_default()
    })
}

/// Clear chat (the web's `clearSandbox`): history, results, the draft.
fn clear(ctx: &Ctx) {
    let store = ctx.store;
    stop_player();
    store.sandbox_ws.reset();
    store.sandbox.set(Loadable::NotAsked);
    store.notice.set(Some("sandbox chat cleared".into()));
}

/// The one Generate path (g, Enter in the prompt, the button). Every
/// refusal NAMES its reason; the synchronous Loading write is the
/// double-press guard (a second paid call must never fire).
fn run(ctx: &Ctx, prov_ix: Signal<usize>, model_ix: Signal<usize>, prompt_state: &TextAreaState) {
    let store = ctx.store;
    let ws = store.sandbox_ws;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        store.notice.set(Some(
            "connect to the gateway first — the sandbox runs real generations".into(),
        ));
        return;
    }
    if store.sandbox.with_untracked(Loadable::is_loading)
        || ws.media.with_untracked(Loadable::is_loading)
    {
        store
            .notice
            .set(Some("a test is already running — one at a time".into()));
        return;
    }
    let mode = ws.mode.get_untracked();
    if mode != SbMode::Text {
        run_media(ctx, mode, prompt_state);
        return;
    }
    let names = crate::ui::providers::provider_names(&store);
    let ix = prov_ix.get_untracked();
    if ix == 0 || ix > names.len() {
        store.notice.set(Some(
            "pick a provider first — Tab reaches the picker".into(),
        ));
        return;
    }
    let name = names[ix - 1].clone();
    let list = store
        .models
        .with_untracked(|m| m.get(&name).and_then(|l| l.ready().cloned()));
    let model = match list {
        Some(models) if !models.is_empty() => {
            let mix = model_ix.get_untracked();
            if mix == 0 || mix > models.len() {
                store
                    .notice
                    .set(Some("pick a model — the list is loaded".into()));
                return;
            }
            models[mix - 1].clone()
        }
        _ => {
            let custom = ctx.ui.sb_model_custom.get_untracked().trim().to_string();
            if custom.is_empty() {
                store
                    .notice
                    .set(Some("pick or type a model id before generating".into()));
                return;
            }
            custom
        }
    };
    let prompt = ctx.ui.sb_prompt.get_untracked();
    if prompt.trim().is_empty() {
        store.notice.set(Some(
            "type a prompt — the test sends it to the model".into(),
        ));
        return;
    }
    // Fold the previous answered turn into the history the web sends as
    // `messages` (only a SUCCESSFUL answer becomes context).
    if let Loadable::Ready(o) = store.sandbox.get_untracked() {
        let last = ws.last_prompt.get_untracked();
        if o.ok && !last.is_empty() {
            ws.history.update(|h| h.push((last, o.response.clone())));
        }
    }
    ws.last_prompt.set(prompt.trim().to_string());
    let controls = TextControls {
        system_prompt: ws.system.get_untracked(),
        reasoning_ix: ws.reasoning_ix.get_untracked(),
        mtp_ix: ws.mtp_ix.get_untracked(),
        history: ws.history.get_untracked(),
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let body = text_body(&name, &model, prompt.trim(), &controls, &utc_iso(now));
    store.sandbox.set(Loadable::Loading);
    ws.text_meta.set(None);
    ctx.send(Cmd::SandboxTest {
        provider: name,
        model,
        prompt,
        request: crate::worker::Body(body),
    });
    // The web empties the composer once a turn is sent.
    prompt_state.set_text(String::new());
    ctx.ui.sb_prompt.set(String::new());
}

fn run_media(ctx: &Ctx, mode: SbMode, prompt_state: &TextAreaState) {
    let store = ctx.store;
    let ws = store.sandbox_ws;
    let rows = match store.routes.get_untracked() {
        Loadable::Ready(d) => d.rows,
        Loadable::Failed(e) => {
            store.notice.set(Some(format!(
                "capability routes unavailable ({}) — press r on Routes (3)",
                e.message
            )));
            return;
        }
        _ => {
            store.notice.set(Some(
                "capability routes are still loading — try again in a moment".into(),
            ));
            return;
        }
    };
    let Some(row) = mode_row(&rows, mode) else {
        store.notice.set(Some(format!(
            "{} ({}) is not offered by this gateway",
            mode.label(),
            mode.route_key()
        )));
        return;
    };
    let Some((provider, model)) = row_pair(row) else {
        store.notice.set(Some(format!(
            "{} is not configured — configure it on Routes (3) first",
            mode.route_key()
        )));
        return;
    };
    let prompt = ctx.ui.sb_prompt.get_untracked().trim().to_string();
    if prompt.is_empty() {
        store.notice.set(Some(format!(
            "type a prompt — {}",
            mode.placeholder().split(" — ").next().unwrap_or("")
        )));
        return;
    }
    let (tenant, user) = store.conn.with_untracked(|c| match c {
        ConnPhase::Connected(id) => (id.tenant_id.clone(), id.user_id.clone()),
        _ => (String::new(), String::new()),
    });
    let run_id = sandbox_run_id(&tenant, &user);
    let request_id = format!("sandbox_{}", crate::worker::next_op());
    let voice = if mode == SbMode::Voice {
        row_voice(row)
    } else {
        None
    };
    let Some((leaf, body)) = media_body(
        mode,
        &provider,
        &model,
        voice.as_deref(),
        &prompt,
        &request_id,
    ) else {
        return;
    };
    ws.media.set(Loadable::Loading);
    ctx.send(Cmd::SandboxMedia {
        request: MediaRequest {
            mode,
            provider,
            model,
            run_id,
            leaf: leaf.to_string(),
            body,
            dest_dir: artifact_dir(),
        },
    });
    prompt_state.set_text(String::new());
    ctx.ui.sb_prompt.set(String::new());
}

/// The pinned text outcome header: state + the PAIR it belongs to.
fn text_status(
    t: &TokenSet,
    s: &Loadable<SandboxOutcome>,
    meta: Option<&TextMeta>,
    turns: usize,
) -> View {
    match s {
        Loadable::NotAsked => line(vec![span(
            "no test run yet — pick a provider and model, then Generate",
            t.text_faint,
        )]),
        Loadable::Loading => line(vec![span(
            "⟳ generating… (a real model call — can take tens of seconds)",
            t.info,
        )]),
        Loadable::Failed(e) => error_panel_hint(t, e, Some("press g / Generate to retry")),
        Loadable::Ready(o) if o.ok => {
            let routed = match (&o.routed_provider, &o.profile) {
                (Some(rp), Some(pf)) => format!("routed via {rp} (profile {pf})"),
                (Some(rp), None) => format!("routed via {rp}"),
                _ => "routing not reported".to_string(),
            };
            let detail = match meta {
                Some(m) => {
                    let mut parts = Vec::new();
                    if !m.usage_label.is_empty() {
                        parts.push(m.usage_label.clone());
                    }
                    parts.push(m.speculation.clone());
                    if turns > 0 {
                        parts.push(format!("{turns} earlier turn(s) sent as context"));
                    }
                    parts.join(" · ")
                }
                None => format!("usage: {}", o.usage.clone().unwrap_or_else(|| "—".into())),
            };
            Element::new()
                .style(LayoutStyle::column())
                .child(line(vec![
                    span_bold("✓ ", t.ok),
                    span_bold(format!("{} / {}", o.provider, o.model), t.text),
                    span(format!("  {routed}"), t.text_muted),
                ]))
                .child(line(vec![span(format!("  {detail}"), t.text_faint)]))
                .build()
        }
        Loadable::Ready(o) => line(vec![span_bold(
            format!("✗ {} / {} — gateway reports ok:false", o.provider, o.model),
            t.error,
        )]),
    }
}

/// The text body: earlier turns, then the latest answer (with the web's
/// reasoning block above it), typeset as markdown. Errors stay plain.
fn text_body_view(gcx: Scope, t: &TokenSet, store: &Store) -> View {
    let ws = store.sandbox_ws;
    let width = abstracttui::app::use_viewport(gcx).get().w;
    let wrap_w = (width as usize).saturating_sub(6).max(20);
    let outcome = store.sandbox.get();
    if let Loadable::Ready(o) = &outcome {
        if !o.ok {
            let text = o
                .error
                .clone()
                .unwrap_or_else(|| "no error detail in the payload".into());
            let rows: Vec<View> = wrap_text(&text, wrap_w)
                .into_iter()
                .map(|l| line(vec![span(l, t.text)]))
                .collect();
            return Scroll::new(
                Element::new()
                    .style(LayoutStyle::column())
                    .children(rows)
                    .build(),
            )
            .scrollbar_auto_hide(true)
            .view(gcx);
        }
    }
    let mut md = String::new();
    for (u, a) in ws.history.get() {
        md.push_str(&format!("**You:** {}\n\n{}\n\n---\n\n", u.trim(), a.trim()));
    }
    if let Loadable::Ready(o) = &outcome {
        let last = ws.last_prompt.get();
        if !md.is_empty() && !last.is_empty() {
            md.push_str(&format!("**You:** {}\n\n", last.trim()));
        }
        if let Some(r) = ws.text_meta.get().and_then(|m| m.reasoning) {
            md.push_str("> **Reasoning**\n>\n");
            for l in r.trim().lines() {
                md.push_str(&format!("> {l}\n"));
            }
            md.push('\n');
        }
        md.push_str(o.response.trim());
    }
    if md.trim().is_empty() {
        return Element::new().style(LayoutStyle::default().h(0)).build();
    }
    Scroll::new(MarkdownView::new(md).view(gcx))
        .scrollbar_auto_hide(true)
        .view(gcx)
}

fn media_status(t: &TokenSet, mode: SbMode, s: &Loadable<MediaOutcome>) -> View {
    match s {
        Loadable::NotAsked => line(vec![span(
            format!(
                "no {} generated yet — type a prompt, then Generate",
                mode.label().to_lowercase()
            ),
            t.text_faint,
        )]),
        Loadable::Loading => line(vec![span(
            "⟳ generating… (local media models can take MINUTES — the gateway keeps going)",
            t.info,
        )]),
        Loadable::Failed(e) => error_panel_hint(t, e, Some("press g / Generate to retry")),
        Loadable::Ready(o) => {
            let secs = o.elapsed_ms as f64 / 1000.0;
            let mut col = Element::new().style(LayoutStyle::column()).child(line(vec![
                span_bold("✓ ", t.ok),
                span_bold(
                    format!("{} · {} / {}", o.mode.label(), o.provider, o.model),
                    t.text,
                ),
                span(
                    format!("  {secs:.1}s · artifact {}", o.artifact.id),
                    t.text_muted,
                ),
            ]));
            col = col.child(match &o.saved {
                Ok((path, n)) => line(vec![
                    span("  saved ", t.text_muted),
                    span(path.display().to_string(), t.text),
                    span(
                        format!(
                            "  ({}{})",
                            human_bytes(*n),
                            if o.artifact.content_type.is_empty() {
                                String::new()
                            } else {
                                format!(", {}", o.artifact.content_type)
                            }
                        ),
                        t.text_muted,
                    ),
                ]),
                Err(e) => line(vec![span(
                    format!("  generated, but saving the file failed: {e}"),
                    t.error,
                )]),
            });
            if o.mode != mode {
                col = col.child(line(vec![span(
                    format!("  (last result is from {} mode)", o.mode.label()),
                    t.text_faint,
                )]));
            }
            col.build()
        }
    }
}

fn media_body_view(gcx: Scope, t: &TokenSet, mode: SbMode, s: &Loadable<MediaOutcome>) -> View {
    let Loadable::Ready(o) = s else {
        return Element::new().style(LayoutStyle::default().h(0)).build();
    };
    if o.mode != mode {
        return Element::new().style(LayoutStyle::default().h(0)).build();
    }
    if let Some(bmp) = &o.image {
        return Image::from_bitmap(bmp.clone())
            .fit(abstracttui::widgets::ImageFit::Contain)
            .layout(LayoutStyle::default().grow(1.0))
            .view(gcx);
    }
    let note = if let Some(n) = &o.image_note {
        n.clone()
    } else if o.mode == SbMode::Video {
        "no terminal video player — open the saved file".to_string()
    } else if o.mode.is_audio() {
        match find_player() {
            Some((p, _)) => format!(
                "Play audio plays it through {}",
                p.file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("the local player")
            ),
            None => {
                "no audio player on PATH (afplay, paplay, aplay or ffplay) — open the saved file"
                    .to_string()
            }
        }
    } else {
        String::new()
    };
    line(vec![span(format!("  {note}"), t.text_muted)])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(
        key: &str,
        provider: Option<&str>,
        model: Option<&str>,
        options: Option<Value>,
    ) -> RouteRow {
        let mut v = json!({"key": key, "kind": "output", "modality": "x", "label": key, "configured": provider.is_some()});
        if let Some(p) = provider {
            v["provider"] = json!(p);
        }
        if let Some(m) = model {
            v["model"] = json!(m);
        }
        if let Some(o) = options {
            v["options"] = o;
        }
        RouteRow::from_value(&v).expect("row parses")
    }

    #[test]
    fn modes_map_to_the_web_route_keys_in_web_order() {
        let keys: Vec<&str> = SbMode::ALL.iter().map(|m| m.route_key()).collect();
        assert_eq!(
            keys,
            [
                "input.text",
                "output.image.text_to_image",
                "output.voice",
                "output.music",
                "output.sound",
                "output.video.text_to_video"
            ]
        );
        for (i, m) in SbMode::ALL.iter().enumerate() {
            assert_eq!(SbMode::from_index(i), *m);
            assert_eq!(m.index(), i);
        }
    }

    #[test]
    fn media_bodies_match_the_web_payloads() {
        let (leaf, b) = media_body(SbMode::Image, "mlx-gen", "flux", None, "a cat", "r1").unwrap();
        assert_eq!(leaf, "images/generate");
        assert_eq!(
            b,
            json!({"prompt": "a cat", "image_provider": "mlx-gen", "image_model": "flux", "request_id": "r1"})
        );

        let (leaf, b) = media_body(
            SbMode::Voice,
            "abstractvoice",
            "kokoro",
            Some("af_heart"),
            "hi",
            "r2",
        )
        .unwrap();
        assert_eq!(leaf, "voice/tts");
        assert_eq!(
            b,
            json!({"text": "hi", "provider": "abstractvoice", "model": "kokoro", "voice": "af_heart", "request_id": "r2"})
        );
        let (_, b) = media_body(SbMode::Voice, "p", "m", None, "hi", "r").unwrap();
        assert!(
            b.get("voice").is_none(),
            "no voice key without a route voice"
        );

        let (leaf, b) = media_body(SbMode::Music, "acestep", "v1", None, "jazz", "r3").unwrap();
        assert_eq!(leaf, "music/generate");
        assert_eq!(b["task"], "text_to_music");
        assert_eq!(b["music_provider"], "acestep");
        assert_eq!(b["music_model"], "v1");
        let (leaf, b) =
            media_body(SbMode::Sound, "acestep", "v1", None, "door slam", "r4").unwrap();
        assert_eq!(leaf, "music/generate");
        assert_eq!(b["task"], "text_to_audio");

        let (leaf, b) = media_body(SbMode::Video, "mlx-gen", "wan", None, "waves", "r5").unwrap();
        assert_eq!(leaf, "videos/generate");
        assert_eq!(
            b,
            json!({"prompt": "waves", "video_provider": "mlx-gen", "video_model": "wan", "request_id": "r5"})
        );

        assert!(media_body(SbMode::Text, "p", "m", None, "x", "r").is_none());
    }

    #[test]
    fn text_body_matches_the_web_payload() {
        let c = TextControls {
            system_prompt: "  be brief ".into(),
            reasoning_ix: 5, // high
            mtp_ix: 3,       // depth 3
            history: vec![("q1".into(), "a1".into())],
        };
        let b = text_body("lmstudio", "qwen", "q2", &c, "2026-09-27T10:00:00Z");
        assert_eq!(b["capability"], "input.text");
        assert_eq!(b["provider"], "lmstudio");
        assert_eq!(b["model"], "qwen");
        assert_eq!(b["prompt"], "q2");
        assert_eq!(b["system_prompt"], "be brief");
        assert_eq!(
            b["messages"],
            json!([{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}])
        );
        assert_eq!(b["attachments"], json!([]));
        assert_eq!(b["reasoning"], "high");
        assert_eq!(
            b["speculation"],
            json!({"mode": "native_mtp", "num_draft_tokens": 3, "require_acceleration": true})
        );
        assert_eq!(b["client_context"]["utc_datetime"], "2026-09-27T10:00:00Z");
        assert!(b.get("max_tokens").is_none(), "the web sends no max_tokens");

        let d = text_body("p", "m", "x", &TextControls::default(), "t");
        assert!(d["system_prompt"].is_null());
        assert!(
            d.get("reasoning").is_none(),
            "default reasoning sends nothing"
        );
        assert!(d.get("speculation").is_none(), "inherit sends nothing");
        let off = TextControls {
            mtp_ix: 1,
            ..Default::default()
        };
        assert_eq!(
            text_body("p", "m", "x", &off, "t")["speculation"],
            json!(false)
        );
    }

    #[test]
    fn utc_iso_formats_known_instants() {
        assert_eq!(utc_iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(utc_iso(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(utc_iso(1_790_503_445), "2026-09-27T10:04:05Z");
    }

    #[test]
    fn media_response_parsing_uses_the_route_shapes() {
        // ImageGenerateResponse (gateway.py ImageGenerateResponse).
        let img = json!({"ok": true, "supported": true, "run_id": "r", "request_id": "q",
            "image_artifact": {"$artifact": "art_1", "content_type": "image/png", "filename": "x.png"},
            "image_artifacts": []});
        let r = parse_media_response(&img).unwrap();
        assert_eq!(r.id, "art_1");
        assert_eq!(r.content_type, "image/png");
        assert_eq!(artifact_ext(&r, SbMode::Image), "png");
        // VoiceTTSResponse: audio_artifact with artifact_id.
        let tts = json!({"ok": true, "run_id": "r", "request_id": "q", "audio_artifact": {"artifact_id": "a2", "content_type": "audio/wav"}});
        let r = parse_media_response(&tts).unwrap();
        assert_eq!(
            (r.id.as_str(), artifact_ext(&r, SbMode::Voice).as_str()),
            ("a2", "wav")
        );
        // MusicGenerateResponse / VideoGenerateResponse.
        let m = json!({"ok": true, "run_id": "r", "request_id": "q", "music_artifact": {"$artifact": "m3", "content_type": "audio/mpeg"}});
        assert_eq!(
            artifact_ext(&parse_media_response(&m).unwrap(), SbMode::Music),
            "mp3"
        );
        let v = json!({"ok": true, "run_id": "r", "request_id": "q", "video_artifact": {"id": "v4"}, "video_artifacts": []});
        let r = parse_media_response(&v).unwrap();
        assert_eq!(
            (r.id.as_str(), artifact_ext(&r, SbMode::Video).as_str()),
            ("v4", "mp4")
        );
        // ok:false — the web's error || code || "Generation failed.".
        let bad = json!({"ok": false, "supported": false, "run_id": "r", "request_id": "q",
            "code": "generated_image_unavailable", "error": "Gateway runtime does not expose AbstractCore durable media helpers."});
        assert_eq!(
            parse_media_response(&bad).unwrap_err(),
            "Gateway runtime does not expose AbstractCore durable media helpers."
        );
        let bad =
            json!({"ok": false, "run_id": "r", "request_id": "q", "code": "music_unavailable"});
        assert_eq!(parse_media_response(&bad).unwrap_err(), "music_unavailable");
        assert_eq!(
            parse_media_response(&json!({"ok": false})).unwrap_err(),
            "Generation failed."
        );
        // Success without a ref is NOT silently fine.
        assert!(parse_media_response(&json!({"ok": true, "run_id": "r"})).is_err());
        assert!(parse_media_response(&json!({"ok": true, "image_artifact": {}})).is_err());
    }

    #[test]
    fn text_meta_mirrors_the_web_labels() {
        let v = json!({"ok": true, "response": "hi", "reasoning": "thought",
            "usage": {"completion_tokens": 40}, "speculation": {"used": true, "num_draft_tokens": 3}});
        let m = TextMeta::from_value(&v, 2000);
        assert_eq!(m.reasoning.as_deref(), Some("thought"));
        assert_eq!(m.usage_label, "2.0s · 40 tok · 20 tok/s");
        assert_eq!(m.speculation, "MTP used (depth 3)");
        let v = json!({"reasoning": null, "speculation": {"used": false, "reason": "not loaded"}});
        let m = TextMeta::from_value(&v, 500);
        assert_eq!(m.reasoning, None);
        assert_eq!(m.usage_label, "500ms");
        assert_eq!(m.speculation, "MTP not used: not loaded");
        assert_eq!(speculation_summary(None), "MTP execution not reported");
    }

    #[test]
    fn route_rows_resolve_like_the_web() {
        let rows = vec![
            row(
                "output.image.text_to_image",
                Some("mlx-gen"),
                Some("flux"),
                None,
            ),
            row(
                "output.voice",
                Some("abstractvoice"),
                Some("kokoro"),
                Some(json!({"profile": "bella"})),
            ),
            row("output.music", None, None, None),
        ];
        let img = mode_row(&rows, SbMode::Image).unwrap();
        assert_eq!(row_pair(img), Some(("mlx-gen".into(), "flux".into())));
        let voice = mode_row(&rows, SbMode::Voice).unwrap();
        assert_eq!(row_voice(voice).as_deref(), Some("bella"));
        assert!(
            row_pair(mode_row(&rows, SbMode::Music).unwrap()).is_none(),
            "unset = not configured"
        );
        assert!(
            mode_row(&rows, SbMode::Video).is_none(),
            "absent row = not offered"
        );
        assert_eq!(
            mode_option_label(SbMode::Image, Some(&rows)),
            "Image — flux"
        );
        assert_eq!(
            mode_option_label(SbMode::Music, Some(&rows)),
            "Music — not configured"
        );
        assert_eq!(
            mode_option_label(SbMode::Video, Some(&rows)),
            "Video — not offered"
        );
    }

    #[test]
    fn run_id_is_the_web_shape() {
        assert_eq!(
            sandbox_run_id("default", "Admin User"),
            "session_memory_gateway_console_sandbox_default_admin_user"
        );
        assert_eq!(
            sandbox_run_id("", ""),
            "session_memory_gateway_console_sandbox_default_user"
        );
    }

    #[test]
    fn human_bytes_reads() {
        assert_eq!(human_bytes(900), "900 B");
        assert_eq!(human_bytes(1536), "1.5 KB");
        assert_eq!(human_bytes(3 * 1024 * 1024), "3.0 MB");
    }
}
