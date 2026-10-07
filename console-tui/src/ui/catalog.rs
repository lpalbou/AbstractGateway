//! Models — the web console's Models page (console_catalog.py, round 5
//! R5.2) in the terminal: ONE list, one block per model (a header line:
//! name · organisation · parameters · licence, the capability tags and the
//! Starter / Hugging Face badges) with one row per artifact (engine · id ·
//! quantization · size · weights and fit · the action), and the downloaded
//! models no catalog entry knows as plain rows under "Not in the catalog"
//! in the same list.
//!
//! Same routes and bodies as the web page, through the plain JSON lane:
//! `GET /models/catalog` (+ `?q=&hub=true` in Hugging Face mode),
//! `GET /models/installed`, `GET /config/capability-defaults` (the current
//! default text model), `GET /models/downloads` (running downloads on
//! open), `POST /models/download {provider, artifact[, expected_bytes]}`
//! followed by `GET /models/download/{id}` every 1.5 s,
//! `POST /models/download/{id}/cancel {"via":"console"}`,
//! `POST /models/delete-download {provider, artifact, dry_run}` (a dry run
//! first: its size is the confirmation sentence), and
//! `PUT /config/capability-defaults/output/text {provider, model,
//! base_url:"", reasoning:"", options:{}}` for "Use as default".
//!
//! The filters are the web filter bar's: search, Catalog / Hugging Face,
//! "Fits this computer", Quantization (All / 4-bit / 8-bit / Other),
//! Provider, Capability and Status (All / Downloaded / Not downloaded),
//! each chip with its count. Filters hide, they never cap. Every sentence
//! (empty states, errors, confirmations, refusals) is the web page's.
//!
//! The page's own state (filters, selection, the delete in progress…)
//! lives in a thread-local so it survives a tab switch like the web
//! page's `mcStore`; a reconnect (the catalog slot back to NotAsked)
//! starts it afresh.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};
use abstracttui::widgets::TextInput;
use serde_json::{json, Value};

use super::kit::InlineConfirm;
use super::util::{line, span, span_bold, wrap_text};
use super::Ctx;
use crate::api::{urlencode, ApiError};
use crate::store::json::WriteState;
use crate::store::{ConnPhase, Loadable, Store};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

// ---------------------------------------------------------------------------
// Keys of the JSON lane
// ---------------------------------------------------------------------------

pub const K_CATALOG: &str = "catalog";
pub const K_INSTALLED: &str = "catalog.installed";
pub const K_DEFAULTS: &str = "catalog.defaults";
pub const K_DOWNLOADS: &str = "catalog.downloads";
pub const K_HUB: &str = "catalog.hub";
/// Prefix of a polled download job's read (`catalog.job:<id>`).
pub const K_JOB: &str = "catalog.job:";
/// Write keys.
pub const W_DOWNLOAD: &str = "catalog.download:";
pub const W_CANCEL: &str = "catalog.cancel:";
pub const W_PLAN: &str = "catalog.delplan:";
pub const W_DELETE: &str = "catalog.delete:";
pub const W_DEFAULT: &str = "catalog.default";

pub const DELETE_URL: &str = "/models/delete-download";
const JOB_POLL: std::time::Duration = std::time::Duration::from_millis(1500);

// ---------------------------------------------------------------------------
// Vocabulary (console_catalog.py constants, verbatim)
// ---------------------------------------------------------------------------

const QUANT_CLASSES: [&str; 9] = [
    "2bit", "3bit", "4bit", "5bit", "6bit", "8bit", "16bit", "full", "unknown",
];
fn quant_label(c: &str) -> &str {
    match c {
        "2bit" => "2-bit",
        "3bit" => "3-bit",
        "4bit" => "4-bit",
        "5bit" => "5-bit",
        "6bit" => "6-bit",
        "8bit" => "8-bit",
        "16bit" => "16-bit",
        "full" => "Full precision",
        "unknown" => "Not stated",
        other => other,
    }
}
pub const QUANT_CHIPS: [(&str, &str); 4] = [
    ("all", "All"),
    ("4bit", "4-bit"),
    ("8bit", "8-bit"),
    ("other", "Other"),
];
pub const STATUS_CHIPS: [(&str, &str); 3] = [
    ("all", "All"),
    ("downloaded", "Downloaded"),
    ("not_downloaded", "Not downloaded"),
];
pub const CAPS: [(&str, &str); 9] = [
    ("text", "Text"),
    ("thinking", "Thinking"),
    ("tools", "Tools"),
    ("vision", "Vision"),
    ("audio", "Audio"),
    ("embedding", "Embedding"),
    ("voice", "Voice"),
    ("image", "Image"),
    ("video", "Video"),
];
pub fn provider_label(p: &str) -> String {
    match p {
        "ollama" => "Ollama",
        "lmstudio" => "LM Studio",
        "mlx" => "MLX",
        "mlx-gen" => "MLX images & video",
        "mlx-vlm" => "MLX vision",
        "huggingface" => "Hugging Face",
        "diffusers" => "Diffusers",
        "supertonic" => "Supertonic",
        "llamacpp" => "llama.cpp",
        other => other,
    }
    .to_string()
}
fn weights_label(status: &str) -> (&'static str, Tone) {
    match status {
        "installed" => ("Downloaded", Tone::Ok),
        "absent" => ("Not downloaded", Tone::Muted),
        "not_applicable" => ("Remote", Tone::Muted),
        _ => ("Unknown", Tone::Muted),
    }
}
fn fit_label(verdict: &str) -> (&'static str, Tone) {
    match verdict {
        "fits" => ("Fits", Tone::Ok),
        "tight" => ("Tight", Tone::Warn),
        "needs_gpu_limit" => ("Needs GPU limit", Tone::Warn),
        "partial_offload" => ("Partial offload", Tone::Warn),
        "too_large" => ("Too large", Tone::Err),
        _ => ("Fit unknown", Tone::Muted),
    }
}
const FITS_FILTER_VERDICTS: [&str; 3] = ["fits", "tight", "needs_gpu_limit"];
const HF_FAMILY: [&str; 8] = [
    "mlx",
    "huggingface",
    "mlx-vlm",
    "mlx-gen",
    "diffusers",
    "transformers",
    "mflux",
    "llamacpp",
];
fn phase_label(phase: &str) -> &str {
    match phase {
        "queued" => "Waiting to start",
        "resolving" => "Preparing",
        "downloading" => "Downloading",
        "verifying" => "Checking files",
        "installing" => "Installing",
        "running" => "Working",
        "stalled" => "Stalled",
        "done" => "Done",
        "failed" => "Failed",
        "cancelled" => "Cancelled",
        other => other,
    }
}
fn state_pill(phase: &str) -> (&str, Tone) {
    match phase {
        "queued" | "pending" => ("Waiting", Tone::Info),
        "resolving" => ("Preparing", Tone::Info),
        "downloading" => ("Downloading", Tone::Info),
        "verifying" => ("Checking files", Tone::Info),
        "installing" => ("Installing", Tone::Info),
        "stalled" => ("Stalled", Tone::Warn),
        "done" => ("Ready", Tone::Ok),
        "failed" => ("Failed", Tone::Err),
        "cancelled" => ("Cancelled", Tone::Muted),
        other => (phase_label(other), Tone::Info),
    }
}

// ---------------------------------------------------------------------------
// Small readers
// ---------------------------------------------------------------------------

/// The tone of a line or chip (mapped to theme ink when drawn).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Text,
    Strong,
    Muted,
    Faint,
    Accent,
    Ok,
    Warn,
    Err,
    Info,
}

fn sv<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}
fn num(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(Value::as_f64).filter(|n| n.is_finite())
}

/// Bytes in DECIMAL units, the console's `uiBytes`.
pub fn ui_bytes(n: f64) -> String {
    let units = ["B", "kB", "MB", "GB", "TB"];
    let mut v = n;
    let mut i = 0;
    while v >= 1000.0 && i < units.len() - 1 {
        v /= 1000.0;
        i += 1;
    }
    if i == 0 {
        format!("{} B", v.round() as i64)
    } else if v >= 100.0 {
        format!("{:.0} {}", v, units[i])
    } else {
        format!("{:.1} {}", v, units[i])
    }
}

/// The console's `uiDuration`.
pub fn ui_duration(s: f64) -> String {
    if s < 0.0 {
        return String::new();
    }
    if s < 60.0 {
        return format!("{} s", s.round().max(1.0) as i64);
    }
    if s < 3600.0 {
        return format!("{} min", (s / 60.0).round() as i64);
    }
    let h = (s / 3600.0).floor() as i64;
    let m = ((s - h as f64 * 3600.0) / 60.0).round() as i64;
    if m > 0 {
        format!("{h} h {m} min")
    } else {
        format!("{h} h")
    }
}

/// `mcParams`.
pub fn params(n: Option<f64>) -> String {
    let Some(n) = n.filter(|n| *n > 0.0) else {
        return String::new();
    };
    if n >= 1e9 {
        let s = if n >= 1e10 {
            format!("{:.0}", n / 1e9)
        } else {
            format!("{:.1}", n / 1e9)
        };
        format!("{}B", s.strip_suffix(".0").unwrap_or(&s))
    } else {
        format!("{}M", (n / 1e6).round() as i64)
    }
}

pub fn key(provider: &str, artifact: &str) -> String {
    format!("{provider}/{artifact}")
}
fn art_key(a: &Value) -> String {
    key(sv(a, "provider"), sv(a, "artifact"))
}

/// The served model id: the artifact minus LM Studio's `@quant` suffix
/// (console.py `servedModelId`).
pub fn served_model_id(provider: &str, artifact: &str) -> String {
    if provider == "lmstudio" {
        if let Some(i) = artifact.rfind('@') {
            return artifact[..i].to_string();
        }
    }
    artifact.to_string()
}

pub fn job_id(job: &Value) -> String {
    let j = sv(job, "job");
    if j.is_empty() {
        sv(job, "job_id").to_string()
    } else {
        j.to_string()
    }
}
pub fn job_active(job: &Value) -> bool {
    matches!(sv(job, "status"), "running" | "queued")
}
fn job_done(job: &Value) -> bool {
    sv(job, "state") == "done" || sv(job, "status") == "completed"
}
fn job_phase(job: &Value) -> String {
    let st = sv(job, "state").to_lowercase();
    if !st.is_empty() {
        return st;
    }
    match sv(job, "status").to_lowercase().as_str() {
        "completed" => "done".into(),
        s @ ("failed" | "cancelled" | "queued") => s.into(),
        _ => "running".into(),
    }
}
fn job_percent(job: &Value) -> Option<f64> {
    if let Some(p) = num(job, "percent") {
        return Some(p.clamp(0.0, 100.0));
    }
    let (done, total) = job_bytes(job);
    match (done, total) {
        (Some(d), Some(t)) if t > 0.0 => Some((d / t * 100.0).clamp(0.0, 100.0)),
        _ => None,
    }
}
fn job_bytes(job: &Value) -> (Option<f64>, Option<f64>) {
    let done = num(job, "bytes_done").or_else(|| num(job, "downloaded_bytes"));
    let total = num(job, "bytes_total")
        .filter(|t| *t > 0.0)
        .or_else(|| num(job, "total_bytes").filter(|t| *t > 0.0));
    (done, total)
}

/// The progress line of an active job (`uiProgressMarkup`, in one or two
/// lines): label · percent · bytes · speed · ETA, the stall, the message.
pub fn progress_lines(job: &Value) -> Vec<(String, Tone)> {
    let phase = job_phase(job);
    let label = if sv(job, "parent_job").is_empty() {
        phase_label(&phase).to_string()
    } else {
        format!("{} · part of Download all", phase_label(&phase))
    };
    let mut bits = vec![label];
    if let Some(p) = job_percent(job) {
        bits.push(if p < 10.0 {
            format!("{p:.1}%")
        } else {
            format!("{p:.0}%")
        });
    }
    let (done, total) = job_bytes(job);
    let active = !matches!(phase.as_str(), "done" | "failed" | "cancelled");
    match (done, total) {
        (Some(d), Some(t)) => bits.push(format!("{} of {}", ui_bytes(d), ui_bytes(t))),
        (Some(d), None) if d > 0.0 => bits.push(format!(
            "{}{}",
            ui_bytes(d),
            if job.get("size_unknown").and_then(Value::as_bool) == Some(true) {
                " (total size unknown)"
            } else {
                ""
            }
        )),
        _ => {}
    }
    if active {
        if let Some(b) = num(job, "bytes_per_second").filter(|b| *b > 0.0) {
            bits.push(format!("{}/s", ui_bytes(b)));
        }
        if let Some(e) = num(job, "eta_s").filter(|e| *e > 0.0) {
            bits.push(format!("about {} left", ui_duration(e)));
        }
    }
    let mut out = vec![(bits.join(" · "), Tone::Info)];
    if phase == "stalled" {
        let for_s = num(job, "stalled_for_s")
            .map(|s| format!("no data for {}", ui_duration(s)))
            .unwrap_or_else(|| "no data right now".into());
        out.push((format!("Stalled · {for_s} · still trying"), Tone::Warn));
    }
    let msg = sv(job, "message").trim();
    if !msg.is_empty() {
        out.push((msg.to_string(), Tone::Muted));
    }
    let note = sv(job, "size_note").trim();
    if !note.is_empty() {
        out.push((note.to_string(), Tone::Muted));
    }
    out
}

// ---------------------------------------------------------------------------
// Filters and the visible list (mcVisible / mcExtras)
// ---------------------------------------------------------------------------

/// The filter bar (`mcDefaultFilters`). `hf`: None = the curated catalog;
/// Some(query) = Hugging Face mode.
#[derive(Clone, Debug, PartialEq)]
pub struct Filters {
    pub q: String,
    pub quant: String,
    pub provider: String,
    pub cap: String,
    pub status: String,
    pub fits: bool,
    pub hf: Option<String>,
}

impl Default for Filters {
    fn default() -> Filters {
        Filters {
            q: String::new(),
            quant: "all".into(),
            provider: "all".into(),
            cap: "all".into(),
            status: "all".into(),
            fits: false,
            hf: None,
        }
    }
}

impl Filters {
    pub fn hf_mode(&self) -> bool {
        self.hf.is_some()
    }
}

/// What the page knows, read-only, for one render.
pub struct Cv<'a> {
    /// The rows the list draws (the catalog, or the Hub answer).
    pub data: Option<&'a Value>,
    /// The curated catalog (the not-in-catalog rows are measured against it).
    pub catalog: Option<&'a Value>,
    pub installed: Option<&'a Value>,
    pub jobs: &'a HashMap<String, Value>,
    pub deleted: &'a HashSet<String>,
}

pub fn rows_of(d: Option<&Value>) -> Vec<&Value> {
    d.and_then(|d| d.get("rows"))
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|r| r.is_object()).collect())
        .unwrap_or_default()
}
pub fn arts(row: &Value) -> Vec<&Value> {
    row.get("artifacts")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|x| x.is_object()).collect())
        .unwrap_or_default()
}

impl Cv<'_> {
    fn rows(&self) -> Vec<&Value> {
        rows_of(self.data)
    }
    fn job(&self, a: &Value) -> Option<&Value> {
        self.jobs.get(&art_key(a))
    }
    pub fn installed(&self, a: &Value) -> bool {
        let k = art_key(a);
        if self.deleted.contains(&k) {
            return false;
        }
        a.get("presence")
            .map(|p| sv(p, "status") == "installed")
            .unwrap_or(false)
            || self.job(a).is_some_and(job_done)
    }
    fn quant_reported(&self) -> bool {
        let rows = self.rows();
        rows.is_empty()
            || rows.iter().all(|r| {
                arts(r).iter().all(|a| {
                    a.get("quant_class")
                        .and_then(Value::as_str)
                        .is_some_and(|c| QUANT_CLASSES.contains(&c))
                })
            })
    }
    fn installed_rows(&self) -> Vec<&Value> {
        self.installed
            .and_then(|d| d.get("rows"))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter(|r| !sv(r, "provider").is_empty() && !sv(r, "artifact").is_empty())
                    .collect()
            })
            .unwrap_or_default()
    }
    /// `mcCovered`: a catalog artifact for the same files is downloaded.
    fn covered(&self, r: &Value) -> bool {
        rows_of(self.catalog).iter().any(|row| {
            arts(row).iter().any(|a| {
                let downloaded = a
                    .get("presence")
                    .map(|p| sv(p, "status") == "installed")
                    .unwrap_or(false)
                    || self.deleted.contains(&art_key(a))
                    || self.job(a).is_some_and(job_done);
                downloaded && same_files(a, r)
            })
        })
    }
    pub fn is_extra_key(&self, k: &str) -> bool {
        self.installed_rows().iter().any(|r| art_key(r) == k)
            && !rows_of(self.catalog)
                .iter()
                .any(|row| arts(row).iter().any(|a| art_key(a) == k))
    }
}

fn fits(a: &Value) -> bool {
    a.get("supported_on_host").and_then(Value::as_bool) != Some(false)
        && FITS_FILTER_VERDICTS.contains(&a.get("fit").map(|f| sv(f, "verdict")).unwrap_or(""))
}
fn quant_bucket(a: &Value) -> &str {
    match sv(a, "quant_class") {
        "4bit" => "4bit",
        "8bit" => "8bit",
        _ => "other",
    }
}
pub fn row_caps(row: &Value) -> Vec<&'static str> {
    let empty = json!({});
    let c = row.get("capabilities").unwrap_or(&empty);
    let t = |k: &str| c.get(k).and_then(Value::as_bool) == Some(true);
    let mut out = Vec::new();
    if t("text") {
        out.push("text");
    }
    if t("thinking") {
        out.push("thinking");
    }
    if matches!(sv(c, "tools"), "native" | "prompted") {
        out.push("tools");
    }
    if t("vision") {
        out.push("vision");
    }
    if t("audio") && !t("speech_synthesis") {
        out.push("audio");
    }
    if t("embedding") {
        out.push("embedding");
    }
    if t("speech_synthesis") {
        out.push("voice");
    }
    if t("image_generation") {
        out.push("image");
    }
    if t("video_generation") {
        out.push("video");
    }
    out
}
fn cap_label(id: &str) -> &str {
    CAPS.iter()
        .find(|(c, _)| *c == id)
        .map(|(_, l)| *l)
        .unwrap_or(id)
}
fn tokens(q: &str) -> Vec<String> {
    q.to_lowercase()
        .split_whitespace()
        .map(str::to_string)
        .collect()
}
fn search_hit(row: &Value, a: &Value, tokens: &[String]) -> bool {
    if tokens.is_empty() {
        return true;
    }
    let hay = [
        sv(row, "display_name"),
        sv(row, "id"),
        sv(row, "vendor"),
        sv(row, "family"),
        sv(a, "artifact"),
    ]
    .join(" ")
    .to_lowercase();
    tokens.iter().all(|t| hay.contains(t.as_str()))
}
fn norm(x: &str) -> String {
    x.trim().to_lowercase()
}
fn repo_of(id: &str) -> String {
    match id.find(':') {
        Some(i) if id[..i].contains('/') && id.len() > i + 1 => id[..i].to_string(),
        _ => id.to_string(),
    }
}
fn ollama_full(id: &str) -> String {
    if id.contains(':') {
        id.to_string()
    } else {
        format!("{id}:latest")
    }
}
/// `mcSameFiles`: catalog artifact `a` names the files of installed row `r`.
pub fn same_files(a: &Value, r: &Value) -> bool {
    let (ap, rp) = (norm(sv(a, "provider")), norm(sv(r, "provider")));
    let (aa, ra) = (norm(sv(a, "artifact")), norm(sv(r, "artifact")));
    if rp == "ollama" {
        return ap == "ollama" && ollama_full(&aa) == ollama_full(&ra);
    }
    if HF_FAMILY.contains(&rp.as_str()) {
        return HF_FAMILY.contains(&ap.as_str()) && (aa == ra || repo_of(&aa) == ra);
    }
    if rp == "lmstudio" {
        return ap == "lmstudio" && (aa == ra || aa.split('@').next() == ra.split('@').next());
    }
    ap == rp && aa == ra
}

/// One visible model: the row and its matching artifacts (recommended
/// first, then the catalog's order).
pub struct Item<'a> {
    pub row: &'a Value,
    pub arts: Vec<&'a Value>,
}

pub fn visible<'a>(cv: &Cv<'a>, f: &Filters) -> Vec<Item<'a>> {
    let quant_on = cv.quant_reported();
    let toks = if f.hf_mode() {
        Vec::new()
    } else {
        tokens(&f.q)
    };
    let mut out = Vec::new();
    for row in rows_of(cv.data) {
        if f.cap != "all" && !row_caps(row).contains(&f.cap.as_str()) {
            continue;
        }
        let all = arts(row);
        let ordered: Vec<&Value> = all
            .iter()
            .filter(|a| a.get("recommended").and_then(Value::as_bool) == Some(true))
            .chain(
                all.iter()
                    .filter(|a| a.get("recommended").and_then(Value::as_bool) != Some(true)),
            )
            .copied()
            .collect();
        let matching: Vec<&Value> = ordered
            .into_iter()
            .filter(|a| {
                if quant_on && f.quant != "all" && quant_bucket(a) != f.quant {
                    return false;
                }
                if f.provider != "all" && sv(a, "provider") != f.provider {
                    return false;
                }
                if f.fits && !fits(a) {
                    return false;
                }
                if f.status == "downloaded" && !cv.installed(a) {
                    return false;
                }
                if f.status == "not_downloaded" && cv.installed(a) {
                    return false;
                }
                search_hit(row, a, &toks)
            })
            .collect();
        if !matching.is_empty() {
            out.push(Item {
                row,
                arts: matching,
            });
        }
    }
    out
}

/// `mcExtras`: the not-in-catalog rows shown with filters `f`.
pub fn extras<'a>(cv: &'a Cv<'a>, f: &Filters) -> Vec<&'a Value> {
    if f.hf_mode() || f.status == "not_downloaded" || f.cap != "all" || f.fits {
        return Vec::new();
    }
    if f.quant != "all" && f.quant != "other" && cv.quant_reported() {
        return Vec::new();
    }
    let toks = tokens(&f.q);
    cv.installed_rows()
        .into_iter()
        .filter(|r| {
            if cv.deleted.contains(&art_key(r)) {
                return false;
            }
            if f.provider != "all" && sv(r, "provider") != f.provider {
                return false;
            }
            if !toks.is_empty() {
                let hay = [sv(r, "artifact"), sv(r, "provider"), sv(r, "catalog_id")]
                    .join(" ")
                    .to_lowercase();
                if !toks.iter().all(|t| hay.contains(t.as_str())) {
                    return false;
                }
            }
            !cv.covered(r)
        })
        .collect()
}

fn count_arts(list: &[Item]) -> usize {
    list.iter().map(|i| i.arts.len()).sum()
}

/// A chip's count = the artifacts the view would show with that chip on.
pub fn chip_count(cv: &Cv, f: &Filters, group: &str, value: &str) -> usize {
    let mut g = f.clone();
    match group {
        "quant" => g.quant = value.into(),
        "provider" => g.provider = value.into(),
        "cap" => g.cap = value.into(),
        _ => g.status = value.into(),
    }
    count_arts(&visible(cv, &g)) + extras(cv, &g).len()
}

/// `mcCountMarkup`: "12 of 82 models · 30 artifacts shown".
pub fn count_text(cv: &Cv, list: &[Item], extras_n: usize) -> String {
    let total = cv.rows().len();
    let a = count_arts(list) + extras_n;
    format!(
        "{} of {} {} · {} {} shown",
        list.len(),
        total,
        if total == 1 { "model" } else { "models" },
        a,
        if a == 1 { "artifact" } else { "artifacts" }
    )
}

/// `mcActiveMarkup`: the filters in use, in words.
pub fn active_words(cv: &Cv, f: &Filters) -> Vec<String> {
    let mut out = Vec::new();
    match &f.hf {
        Some(q) if !q.is_empty() => out.push(format!("Hugging Face: “{q}”")),
        Some(_) => out.push("Hugging Face".into()),
        None if !f.q.is_empty() => out.push(format!("“{}”", f.q)),
        None => {}
    }
    if f.quant != "all" && cv.quant_reported() {
        out.push(
            QUANT_CHIPS
                .iter()
                .find(|c| c.0 == f.quant)
                .map(|c| c.1.to_string())
                .unwrap_or_else(|| f.quant.clone()),
        );
    }
    if f.provider != "all" {
        out.push(provider_label(&f.provider));
    }
    if f.cap != "all" {
        out.push(cap_label(&f.cap).to_string());
    }
    if f.status != "all" {
        out.push(
            STATUS_CHIPS
                .iter()
                .find(|c| c.0 == f.status)
                .map(|c| c.1.to_string())
                .unwrap_or_else(|| f.status.clone()),
        );
    }
    out
}

/// The providers the Provider chips offer (catalog order, then the
/// not-in-catalog engines, then a chosen one no row names).
pub fn providers(cv: &Cv, f: &Filters) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for r in cv.rows() {
        for a in arts(r) {
            let p = sv(a, "provider");
            if !p.is_empty() && !out.iter().any(|x| x == p) {
                out.push(p.to_string());
            }
        }
    }
    let mut all = f.clone();
    all.provider = "all".into();
    for r in extras(cv, &all) {
        let p = sv(r, "provider");
        if !out.iter().any(|x| x == p) {
            out.push(p.to_string());
        }
    }
    if f.provider != "all" && !out.contains(&f.provider) {
        out.push(f.provider.clone());
    }
    out
}

/// The capabilities the Capability chips offer.
pub fn caps_offered(cv: &Cv, f: &Filters) -> Vec<&'static str> {
    CAPS.iter()
        .filter(|(id, _)| cv.rows().iter().any(|r| row_caps(r).contains(id)) || f.cap == *id)
        .map(|(id, _)| *id)
        .collect()
}

/// `mcFitTitle`: the fit's facts, one per line.
pub fn fit_title(fit: Option<&Value>) -> Vec<String> {
    let empty = json!({});
    let f = fit.unwrap_or(&empty);
    let mut lines = Vec::new();
    match (num(f, "need_bytes"), num(f, "usable_bytes"), num(f, "ceiling_bytes")) {
        (Some(n), Some(u), _) => lines.push(format!(
            "Needs about {} of the {} this computer can give a model",
            ui_bytes(n),
            ui_bytes(u)
        )),
        (Some(n), None, Some(c)) => lines.push(format!(
            "Needs about {}; this computer lets a model use at most {}, minus what the system keeps free",
            ui_bytes(n),
            ui_bytes(c)
        )),
        _ => {}
    }
    if let Some(fr) = num(f, "free_now_bytes") {
        lines.push(format!("Free right now: {}", ui_bytes(fr)));
    }
    if f.get("disk_ok").and_then(Value::as_bool) == Some(false) {
        lines.push("Not enough free disk space for the download".into());
    }
    if let Some(m) = num(f, "max_context") {
        lines.push(format!(
            "Longest context that fits: {} tokens",
            group_thousands(m as i64)
        ));
    }
    for n in f
        .get("notes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(s) = n.as_str() {
            lines.push(s.to_string());
        }
    }
    lines
}

fn group_thousands(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

/// `mcGpuLimitMarkup`, in words (None when not `needs_gpu_limit`).
pub fn gpu_limit_note(a: &Value) -> Option<String> {
    let f = a.get("fit")?;
    if sv(f, "verdict") != "needs_gpu_limit" {
        return None;
    }
    let gl = f.get("gpu_limit")?;
    let cmd = sv(gl, "command");
    if cmd.is_empty() {
        return None;
    }
    let gib = num(gl, "required_mb")
        .map(|mb| format!(" {} GiB", (mb / 1024.0).round() as i64))
        .unwrap_or_else(|| " more memory".into());
    Some(format!(
        "It fits once macOS lets the GPU use{gib}: run {cmd} in a terminal (asks for your password; lasts until the Mac restarts), then load it."
    ))
}

/// The current default text model (`mcCurrentDefault`) from
/// `GET /config/capability-defaults`.
pub fn current_default(defaults: Option<&Value>) -> Option<(String, String)> {
    let row = defaults?
        .get("routes")?
        .as_array()?
        .iter()
        .find(|r| sv(r, "key") == "output.text")?;
    let (p, m) = (sv(row, "provider"), sv(row, "model"));
    (!p.is_empty() && !m.is_empty()).then(|| (p.to_string(), m.to_string()))
}

fn can_be_default(row: &Value) -> bool {
    let empty = json!({});
    let c = row.get("capabilities").unwrap_or(&empty);
    c.get("text").and_then(Value::as_bool) == Some(true)
        && c.get("embedding").and_then(Value::as_bool) != Some(true)
}

/// The delete confirmation sentence from a dry run (`mcDeleteConfirmMarkup`).
pub fn confirm_sentence(plan: &Value) -> String {
    let size = match num(plan, "freed_bytes") {
        Some(b) => format!("Deletes {} from this computer.", ui_bytes(b)),
        None => "Deletes this model's files from this computer.".to_string(),
    };
    let also: Vec<&str> = plan
        .get("also_used_by")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let also = if also.is_empty() {
        String::new()
    } else {
        format!(" {} uses the same files.", also.join(" and "))
    };
    format!("{size} Files only — nothing in your runs is touched.{also}")
}

/// `mcDeleteNotice`: the gateway's message + fix; a refusal is a warning.
pub fn delete_notice(e: &ApiError) -> (Tone, String) {
    let d = e.body.clone().unwrap_or(Value::Null);
    let msg = d
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| e.message.clone());
    let fix = sv(&d, "fix");
    let text = if fix.is_empty() {
        msg
    } else {
        format!("{msg} {fix}")
    };
    let tone = if sv(&d, "status") == "refused" {
        Tone::Warn
    } else {
        Tone::Err
    };
    (tone, text)
}

fn err_text(e: &ApiError) -> String {
    e.body
        .as_ref()
        .and_then(|b| b.get("message").or_else(|| b.get("detail")))
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| e.message.clone())
}

// ---------------------------------------------------------------------------
// Page state (survives a tab switch, like the web page's mcStore)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub enum DelPhase {
    Checking,
    Confirm(Value),
    Deleting,
}

#[derive(Default)]
pub struct PageState {
    pub filters: Filters,
    /// The selected artifact row (provider/artifact key).
    pub sel: Option<String>,
    pub expanded: Option<String>,
    pub del: HashMap<String, DelPhase>,
    pub deleted: HashSet<String>,
    pub notices: HashMap<String, (Tone, String)>,
    pub message: Option<(Tone, String)>,
    pub extra_notice: Option<(Tone, String)>,
    pub busy: HashSet<String>,
    pub jobs: HashMap<String, Value>,
    pub cancelling: HashSet<String>,
    /// The Hub query the K_HUB slot answers.
    pub hub_q: Option<String>,
    /// The catalog was read at least once (a NotAsked slot after that is a
    /// reconnect: the page starts afresh).
    pub loaded: bool,
}

thread_local! {
    static PAGE: RefCell<PageState> = RefCell::new(PageState::default());
}

/// Read the page state.
pub fn with_state<R>(f: impl FnOnce(&PageState) -> R) -> R {
    PAGE.with(|p| f(&p.borrow()))
}
fn edit<R>(f: impl FnOnce(&mut PageState) -> R) -> R {
    PAGE.with(|p| f(&mut p.borrow_mut()))
}
/// A download this page started (or adopted) is still running on the
/// gateway — `q` asks before quitting while one is.
pub fn download_running() -> bool {
    with_state(|p| p.jobs.values().any(job_active))
}

/// Forget the page state (tests; a reconnect).
pub fn reset_state() {
    edit(|p| *p = PageState::default());
}

// ---------------------------------------------------------------------------
// Layout: the list as painted lines (pure, tested)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LKind {
    Model,
    Art,
    Under,
    Section,
    Note,
}

#[derive(Clone, Debug)]
pub struct PLine {
    pub spans: Vec<(String, Tone)>,
    /// The artifact row this line belongs to (selection + clicks).
    pub row: Option<String>,
    pub kind: LKind,
}

impl PLine {
    pub fn text(&self) -> String {
        self.spans.iter().map(|(s, _)| s.as_str()).collect()
    }
}

fn pl(text: impl Into<String>, tone: Tone, kind: LKind, row: Option<String>) -> PLine {
    PLine {
        spans: vec![(text.into(), tone)],
        row,
        kind,
    }
}

/// Column widths for the artifact rows (engine · id · quant · size ·
/// status · action) at `width`: every column at its natural width, the id
/// taking what is left; when the id would get less than its floor, the
/// widest of action / status / engine / quant give cells back down to
/// their own floors (their words then wrap onto a second line — never cut).
pub fn art_widths(rows: &[Vec<String>], width: i32) -> Vec<i32> {
    const FLOORS: [i32; 6] = [12, 16, 6, 12, 21, 18];
    let mut nat = [0i32; 6];
    for r in rows {
        for (i, c) in r.iter().enumerate().take(6) {
            nat[i] = nat[i].max(abstracttui::text::width(c));
        }
    }
    let avail = (width - 2 - 2 * 5).max(30);
    let mut ws = nat;
    for (i, f) in FLOORS.iter().enumerate() {
        ws[i] = ws[i].max((*f).min(nat[i]).max(1));
    }
    let id_floor = FLOORS[1].min(nat[1].max(1));
    loop {
        let others: i32 = ws
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != 1)
            .map(|(_, w)| *w)
            .sum();
        let id = avail - others;
        if id >= id_floor.max(nat[1].min(32)) {
            ws[1] = id.min(nat[1]).max(id_floor);
            break;
        }
        // Give cells back in the web's order of importance: the action
        // words first, then the engine, the quantization, the status
        // chips, the size last.
        let pick = [5usize, 0, 2, 4, 3]
            .into_iter()
            .find(|i| ws[*i] > FLOORS[*i]);
        match pick {
            Some(i) => ws[i] -= 1,
            None => {
                // Every floor reached and still too narrow (80 columns):
                // the widest other column gives cells until the id has 8
                // (words wrap; the row never runs past the width).
                let widest = [5usize, 0, 2, 4, 3]
                    .into_iter()
                    .filter(|i| ws[*i] > 4)
                    .max_by_key(|i| ws[*i]);
                match widest {
                    Some(i) if id < 8 => ws[i] -= 1,
                    _ => {
                        ws[1] = id.max(1);
                        break;
                    }
                }
            }
        }
    }
    ws.to_vec()
}

/// One artifact row's cells (engine · id · quant · size · status · action).
fn art_cells(
    cv: &Cv,
    st: &PageState,
    row: &Value,
    a: &Value,
    alone: bool,
    admin: bool,
    default: &Option<(String, String)>,
) -> Vec<String> {
    let k = art_key(a);
    let job = cv.job(a);
    let installed = cv.installed(a);
    let gone = st.deleted.contains(&k);
    let presence = a.get("presence").map(|p| sv(p, "status")).unwrap_or("");
    let weights = match job {
        Some(j) if job_active(j) => {
            if st.cancelling.contains(&job_id(j)) {
                "Cancelling".to_string()
            } else {
                state_pill(&job_phase(j)).0.to_string()
            }
        }
        _ => {
            let (l, _) = if installed {
                weights_label("installed")
            } else if gone {
                weights_label("absent")
            } else {
                weights_label(presence)
            };
            l.to_string()
        }
    };
    let fitw = if a.get("supported_on_host").and_then(Value::as_bool) == Some(false) {
        "Not for this computer".to_string()
    } else {
        fit_label(a.get("fit").map(|f| sv(f, "verdict")).unwrap_or(""))
            .0
            .to_string()
    };
    let qc = sv(a, "quant_class");
    let quant = if QUANT_CLASSES.contains(&qc) {
        quant_label(qc).to_string()
    } else if !sv(a, "quant").is_empty() {
        sv(a, "quant").to_string()
    } else {
        "Not stated".to_string()
    };
    let exact = matches!(sv(a, "size_source"), "catalog" | "hf_api" | "engine");
    let size = match num(a, "download_bytes") {
        Some(b) => format!("{}{}", if exact { "" } else { "about " }, ui_bytes(b)),
        None => "Size unknown".into(),
    };
    let rec = a.get("recommended").and_then(Value::as_bool) == Some(true) && !alone;
    vec![
        format!(
            "{}{}",
            if rec { "● " } else { "" },
            provider_label(sv(a, "provider"))
        ),
        sv(a, "artifact").to_string(),
        quant,
        size,
        format!("{weights} · {fitw}"),
        action_words(cv, st, row, a, admin, default),
    ]
}

/// `mcActionMarkup`, as the key that does it.
fn action_words(
    cv: &Cv,
    st: &PageState,
    row: &Value,
    a: &Value,
    admin: bool,
    default: &Option<(String, String)>,
) -> String {
    let k = art_key(a);
    if let Some(j) = cv.job(a).filter(|j| job_active(j)) {
        return if st.cancelling.contains(&job_id(j)) {
            "Cancelling...".into()
        } else {
            "c Cancel".into()
        };
    }
    if st.busy.contains(&k) {
        return "Starting...".into();
    }
    if cv.installed(a) {
        let del = delete_word(st, &k, admin);
        if !can_be_default(row) {
            return del;
        }
        let model = served_model_id(sv(a, "provider"), sv(a, "artifact"));
        if default
            .as_ref()
            .is_some_and(|(p, m)| p == sv(a, "provider") && *m == model)
        {
            return join_words("Default text model", &del);
        }
        let use_w = if admin {
            "u Use as default".to_string()
        } else {
            "Use as default (admin only)".to_string()
        };
        return join_words(&use_w, &del);
    }
    if a.get("downloadable").and_then(Value::as_bool) != Some(true) {
        return "Not available here".into();
    }
    let failed = cv.job(a).is_some_and(|j| sv(j, "status") == "failed");
    let label = if failed { "Try again" } else { "Download" };
    if admin {
        format!("w {label}")
    } else {
        format!("{label} (admin only)")
    }
}

fn join_words(a: &str, b: &str) -> String {
    if b.is_empty() {
        a.to_string()
    } else {
        format!("{a} · {b}")
    }
}

fn delete_word(st: &PageState, k: &str, admin: bool) -> String {
    match st.del.get(k) {
        Some(DelPhase::Checking) => "Checking...".into(),
        Some(DelPhase::Deleting) => "Deleting...".into(),
        Some(DelPhase::Confirm(_)) => String::new(),
        None if admin => "d Delete".into(),
        None => "Delete (admin only)".into(),
    }
}

/// The lines under an artifact row (`mcJobMarkup` + the GPU-limit note).
fn under_lines(
    cv: &Cv,
    st: &PageState,
    a: &Value,
    k: &str,
    confirm: Option<(String, String)>,
    width: i32,
) -> Vec<PLine> {
    let mut out = Vec::new();
    let w = (width - 4).max(10) as usize;
    let push = |out: &mut Vec<PLine>, text: &str, tone: Tone| {
        for l in wrap_text(text, w) {
            out.push(pl(
                format!("    {l}"),
                tone,
                LKind::Under,
                Some(k.to_string()),
            ));
        }
    };
    if a.get("supported_on_host").and_then(Value::as_bool) != Some(false) {
        if let Some(n) = gpu_limit_note(a) {
            push(&mut out, &n, Tone::Warn);
        }
    }
    if let Some((sentence, verb)) = confirm {
        push(&mut out, &sentence, Tone::Warn);
        out.push(PLine {
            spans: vec![
                ("    [y] ".into(), Tone::Accent),
                (verb, Tone::Err),
                ("  [n] ".into(), Tone::Accent),
                ("Keep".into(), Tone::Text),
            ],
            row: Some(k.to_string()),
            kind: LKind::Under,
        });
    }
    if let Some(j) = cv.jobs.get(k) {
        if job_active(j) {
            for (l, tone) in progress_lines(j) {
                push(&mut out, &l, tone);
            }
        } else if sv(j, "status") == "failed" {
            let said = {
                let r = sv(j, "ended_reason").trim();
                let m = sv(j, "message").trim();
                if !r.is_empty() {
                    r.to_string()
                } else if !m.is_empty() {
                    m.to_string()
                } else {
                    "Try again.".to_string()
                }
            };
            push(
                &mut out,
                &format!("The download did not finish. {said}"),
                Tone::Err,
            );
            let why = {
                let e = sv(j, "error").trim();
                if e.is_empty() {
                    sv(j, "message").trim().to_string()
                } else {
                    e.to_string()
                }
            };
            if !why.is_empty() && why != said {
                push(&mut out, &why, Tone::Muted);
            }
        } else if sv(j, "status") == "cancelled" && !cv.installed(a) {
            let r = sv(j, "ended_reason");
            push(
                &mut out,
                if r.is_empty() {
                    "Download cancelled. Download it again any time."
                } else {
                    r
                },
                Tone::Muted,
            );
        }
    }
    if let Some((tone, text)) = st.notices.get(k) {
        push(&mut out, text, *tone);
    }
    out
}

/// The expanded row's facts (Enter): the full id, engine, quantization,
/// size source, presence, fit facts, the download command.
fn detail_lines(a: &Value, alone: bool, k: &str, width: i32) -> Vec<PLine> {
    let mut facts: Vec<String> = Vec::new();
    facts.push(format!("Artifact: {}", sv(a, "artifact")));
    let (p, e) = (sv(a, "provider"), sv(a, "engine"));
    facts.push(if !e.is_empty() && e != p {
        format!("{} (runs on {})", provider_label(p), provider_label(e))
    } else {
        provider_label(p)
    });
    let raw = sv(a, "quant");
    let mut q = if raw.is_empty() {
        "The catalog does not name a quantization".to_string()
    } else {
        format!("Quantization: {raw}")
    };
    if let Some(b) = num(a, "bits") {
        q.push_str(&format!(" · {b} bits"));
    }
    facts.push(q);
    let exact = matches!(sv(a, "size_source"), "catalog" | "hf_api" | "engine");
    facts.push(if num(a, "download_bytes").is_some() {
        if exact {
            "Download size".into()
        } else {
            "Estimated from the parameter count and quantization".into()
        }
    } else {
        "The catalog has no size for this build".into()
    });
    if let Some(pr) = a.get("presence") {
        let where_ = [sv(pr, "location"), sv(pr, "evidence")]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
        if !where_.is_empty() {
            facts.push(where_);
        }
    }
    if a.get("recommended").and_then(Value::as_bool) == Some(true) && !alone {
        facts.push("Recommended for this computer".into());
    }
    facts.extend(fit_title(a.get("fit")));
    if !sv(a, "cli_download").is_empty() {
        facts.push(format!("CLI: {}", sv(a, "cli_download")));
    }
    let w = (width - 6).max(10) as usize;
    let mut out = Vec::new();
    for f in facts {
        for l in wrap_text(&f, w) {
            out.push(pl(
                format!("      {l}"),
                Tone::Muted,
                LKind::Under,
                Some(k.to_string()),
            ));
        }
    }
    out
}

/// The extra (not-in-catalog) row's cells (`mcExtraArtMarkup`).
fn extra_cells(st: &PageState, r: &Value, admin: bool) -> Vec<String> {
    let size = num(r, "size_bytes")
        .map(ui_bytes)
        .unwrap_or_else(|| "Size unknown".into());
    let quant = if sv(r, "quant").is_empty() {
        "Not stated".to_string()
    } else {
        sv(r, "quant").to_string()
    };
    let chips = if r.get("loaded").and_then(Value::as_bool) == Some(true) {
        "Downloaded · Loaded"
    } else {
        "Downloaded"
    };
    vec![
        provider_label(sv(r, "provider")),
        sv(r, "artifact").to_string(),
        quant,
        size,
        chips.to_string(),
        delete_word(st, &art_key(r), admin),
    ]
}

/// The narrow row (the web's container query under 1000 px): the engine
/// badge and the id on the first line, the facts and the action under it.
fn stack_cells(cells: &[String], width: i32) -> Vec<String> {
    let mut out = Vec::new();
    let head = format!(
        "{}  {}",
        cells.first().cloned().unwrap_or_default(),
        cells.get(1).cloned().unwrap_or_default()
    );
    for l in wrap_text(&head, (width - 2).max(10) as usize) {
        out.push(format!("  {l}"));
    }
    let facts: Vec<String> = cells
        .iter()
        .skip(2)
        .filter(|c| !c.is_empty())
        .cloned()
        .collect();
    for l in wrap_text(&facts.join(" · "), (width - 4).max(10) as usize) {
        out.push(format!("    {l}"));
    }
    out
}

fn join_cells(cells: &[String], ws: &[i32]) -> Vec<String> {
    let wrapped: Vec<Vec<String>> = cells
        .iter()
        .zip(ws)
        .map(|(c, w)| wrap_text(c, (*w).max(1) as usize))
        .collect();
    let h = wrapped.iter().map(Vec::len).max().unwrap_or(1).max(1);
    (0..h)
        .map(|li| {
            let mut s = String::from("  ");
            for (c, w) in ws.iter().enumerate() {
                let piece = wrapped[c].get(li).cloned().unwrap_or_default();
                let pw = abstracttui::text::width(&piece);
                s.push_str(&piece);
                if c + 1 < ws.len() {
                    s.push_str(&" ".repeat(((*w - pw).max(0) + 2) as usize));
                }
            }
            s.trim_end().to_string()
        })
        .collect()
}

/// Every line of the list (`mcListMarkup`), for `width` cells.
#[allow(clippy::too_many_arguments)]
pub fn list_lines(
    cv: &Cv,
    st: &PageState,
    hub: &Loadable<Value>,
    catalog: &Loadable<Value>,
    installed_err: Option<String>,
    default: &Option<(String, String)>,
    admin: bool,
    confirm: Option<(String, String, String)>,
    width: i32,
) -> Vec<PLine> {
    let f = &st.filters;
    let w = width.max(20);
    let note = |text: &str, tone: Tone| -> Vec<PLine> {
        wrap_text(text, w as usize)
            .into_iter()
            .map(|l| pl(l, tone, LKind::Note, None))
            .collect()
    };
    if let Some(q) = &f.hf {
        if q.is_empty() {
            let mut out = note("Search Hugging Face", Tone::Strong);
            out.extend(note("Type a model name above and press Enter. Results show as cards you can download, like the catalog.", Tone::Muted));
            return out;
        }
        if st.hub_q.as_deref() == Some(q.as_str()) {
            if let Loadable::Failed(e) = hub {
                let mut out = note("Hugging Face could not be searched right now.", Tone::Err);
                out.extend(note(
                    "Check that the gateway host is online, then search again.",
                    Tone::Muted,
                ));
                out.extend(note(&format!("Details: {}", err_text(e)), Tone::Faint));
                return out;
            }
        }
        if cv.data.is_none() {
            return note(&format!("Searching Hugging Face for “{q}”..."), Tone::Muted);
        }
        if cv.rows().is_empty() {
            let mut out = note(
                &format!("Hugging Face has no model matching “{q}”."),
                Tone::Strong,
            );
            out.extend(note("Try another name, or fewer words.", Tone::Muted));
            return out;
        }
    }
    if cv.catalog.is_none() {
        if let Loadable::Failed(e) = catalog {
            let mut out = note("The model catalog did not load.", Tone::Err);
            out.extend(note(&err_text(e), Tone::Muted));
            return out;
        }
        return note("Loading the model catalog...", Tone::Muted);
    }
    let list = visible(cv, f);
    let extra = extras(cv, f);
    let confirm_for = |k: &str| -> Option<(String, String)> {
        confirm
            .as_ref()
            .filter(|(ck, _, _)| ck == k)
            .map(|(_, s, v)| (s.clone(), v.clone()))
    };
    // Column widths solved across EVERY artifact row, so the columns line
    // up from one model to the next (the web's fixed grid).
    let mut cells_by_key: Vec<(String, Vec<String>)> = Vec::new();
    for item in &list {
        let alone = arts(item.row).len() == 1;
        for a in &item.arts {
            cells_by_key.push((
                art_key(a),
                art_cells(cv, st, item.row, a, alone, admin, default),
            ));
        }
    }
    for r in &extra {
        cells_by_key.push((art_key(r), extra_cells(st, r, admin)));
    }
    let all_cells: Vec<Vec<String>> = cells_by_key.iter().map(|(_, c)| c.clone()).collect();
    let ws = art_widths(&all_cells, w);
    let narrow = w < 100;
    let layout_row = |cells: &[String]| -> Vec<String> {
        if narrow {
            stack_cells(cells, w)
        } else {
            join_cells(cells, &ws)
        }
    };
    let cells_of = |k: &str| -> Vec<String> {
        cells_by_key
            .iter()
            .find(|(ck, _)| ck == k)
            .map(|(_, c)| c.clone())
            .unwrap_or_default()
    };
    let mut out: Vec<PLine> = Vec::new();
    let extras_block = |out: &mut Vec<PLine>| {
        let show_err = !f.hf_mode() && f.status != "not_downloaded";
        let err = installed_err.clone().filter(|_| show_err);
        let xnote = if f.hf_mode() {
            None
        } else {
            st.extra_notice.clone()
        };
        if extra.is_empty() && err.is_none() && xnote.is_none() {
            return;
        }
        out.push(pl("Not in the catalog", Tone::Muted, LKind::Section, None));
        if let Some((tone, t)) = xnote {
            for l in wrap_text(&t, w as usize) {
                out.push(pl(l, tone, LKind::Note, None));
            }
        }
        if let Some(e) = err {
            for l in wrap_text(
                "The models outside the catalog could not be listed.",
                w as usize,
            ) {
                out.push(pl(l, Tone::Warn, LKind::Note, None));
            }
            for l in wrap_text(&e, w as usize) {
                out.push(pl(l, Tone::Muted, LKind::Note, None));
            }
        }
        for r in &extra {
            let k = art_key(r);
            for l in layout_row(&cells_of(&k)) {
                out.push(pl(l, Tone::Text, LKind::Art, Some(k.clone())));
            }
            if st.expanded.as_deref() == Some(k.as_str()) {
                let mut facts = vec![format!("Artifact: {}", sv(r, "artifact"))];
                if !sv(r, "location").is_empty() {
                    facts.push(format!("Size on disk · {}", sv(r, "location")));
                }
                for fct in facts {
                    for l in wrap_text(&fct, (w - 6).max(10) as usize) {
                        out.push(pl(
                            format!("      {l}"),
                            Tone::Muted,
                            LKind::Under,
                            Some(k.clone()),
                        ));
                    }
                }
            }
            out.extend(under_lines(cv, st, r, &k, confirm_for(&k), w));
        }
    };
    if list.is_empty() && !extra.is_empty() {
        extras_block(&mut out);
        return out;
    }
    if list.is_empty() {
        out.extend(note("No model matches these filters.", Tone::Strong));
        if cv.rows().is_empty() {
            out.extend(note("This gateway's catalog is empty.", Tone::Muted));
        } else {
            out.extend(note(
                "Change or clear the filters to see the rest of the catalog. (x clears the filters)",
                Tone::Muted,
            ));
        }
        extras_block(&mut out);
        return out;
    }
    for item in &list {
        let row = item.row;
        let mut meta: Vec<String> = Vec::new();
        if !sv(row, "vendor").is_empty() {
            meta.push(sv(row, "vendor").to_string());
        }
        let p = params(num(row, "params_total"));
        let act = params(num(row, "params_active"));
        if !p.is_empty() {
            meta.push(if act.is_empty() {
                format!("{p} params")
            } else {
                format!("{p} params ({act} active)")
            });
        }
        if !sv(row, "license").is_empty() {
            meta.push(sv(row, "license").to_string());
        }
        let mut head = vec![(
            if sv(row, "display_name").is_empty() {
                sv(row, "id").to_string()
            } else {
                sv(row, "display_name").to_string()
            },
            Tone::Strong,
        )];
        if !meta.is_empty() {
            head.push((format!("  {}", meta.join(" · ")), Tone::Muted));
        }
        let caps: Vec<String> = row_caps(row)
            .iter()
            .map(|c| format!("[{}]", cap_label(c)))
            .collect();
        if !caps.is_empty() {
            head.push((format!("  {}", caps.join(" ")), Tone::Faint));
        }
        if row.get("starter").and_then(Value::as_bool) == Some(true) {
            head.push(("  Starter".into(), Tone::Accent));
        }
        if sv(row, "source") == "hf_search" {
            head.push(("  Hugging Face".into(), Tone::Accent));
        }
        // The header wraps as one sentence (never cut).
        let text: String = head.iter().map(|(s, _)| s.as_str()).collect();
        if abstracttui::text::width(&text) <= w {
            out.push(PLine {
                spans: head,
                row: None,
                kind: LKind::Model,
            });
        } else {
            for (i, l) in wrap_text(&text, w as usize).into_iter().enumerate() {
                out.push(pl(
                    l,
                    if i == 0 { Tone::Strong } else { Tone::Muted },
                    LKind::Model,
                    None,
                ));
            }
        }
        let alone = arts(row).len() == 1;
        for a in &item.arts {
            let k = art_key(a);
            let primary = a.get("recommended").and_then(Value::as_bool) == Some(true);
            for l in layout_row(&cells_of(&k)) {
                out.push(pl(
                    l,
                    if primary { Tone::Strong } else { Tone::Text },
                    LKind::Art,
                    Some(k.clone()),
                ));
            }
            if st.expanded.as_deref() == Some(k.as_str()) {
                out.extend(detail_lines(a, alone, &k, w));
            }
            out.extend(under_lines(cv, st, a, &k, confirm_for(&k), w));
        }
    }
    extras_block(&mut out);
    out
}

/// The selectable rows in order (artifact keys).
pub fn selectable(lines: &[PLine]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for l in lines {
        if l.kind == LKind::Art {
            if let Some(k) = &l.row {
                if out.last() != Some(k) {
                    out.push(k.clone());
                }
            }
        }
    }
    out
}

/// The header block above the list: host facts, the bar, the chips, the
/// notices (`mcHostMarkup`, `mcControlsMarkup`, `mcNoticeMarkup`).
/// The host facts line (`mcHostMarkup` + Check again).
pub fn host_lines(cv: &Cv, loading: bool, width: i32) -> Vec<PLine> {
    let w = width.max(20) as usize;
    let mut out = Vec::new();
    // Host facts.
    if let Some(p) = cv.catalog.and_then(|d| d.get("host_profile")) {
        let mut bits = Vec::new();
        let chip = if !sv(p, "gpu_name").is_empty() {
            sv(p, "gpu_name")
        } else {
            sv(p, "accelerator")
        };
        if !chip.is_empty() {
            bits.push(chip.to_string());
        }
        if let Some(r) = num(p, "ram_bytes") {
            bits.push(format!(
                "{} {}",
                ui_bytes(r),
                if p.get("unified_memory").and_then(Value::as_bool) == Some(true) {
                    "unified memory"
                } else {
                    "memory"
                }
            ));
        }
        if let Some(c) = num(p, "ceiling_bytes") {
            bits.push(format!("models up to about {}", ui_bytes(c)));
        }
        if !bits.is_empty() {
            let t = format!(
                "This computer: {}  · r {}",
                bits.join(" · "),
                if loading {
                    "Checking..."
                } else {
                    "Check again"
                }
            );
            for l in wrap_text(&t, w) {
                out.push(pl(l, Tone::Muted, LKind::Note, None));
            }
        }
    }
    out
}

/// The block under the bar: the count, the active filters, the chips,
/// the notices and the page message.
pub fn header_lines(cv: &Cv, st: &PageState, width: i32) -> Vec<PLine> {
    let f = &st.filters;
    let w = width.max(20) as usize;
    let mut out = Vec::new();
    let have = cv.data.is_some();
    // The bar's right end: count + active filters.
    if have {
        let list = visible(cv, f);
        let ex = extras(cv, f);
        let mut t = count_text(cv, &list, ex.len());
        let act = active_words(cv, f);
        if !act.is_empty() {
            t.push_str(&format!("  · {}", act.join(" · ")));
        }
        for l in wrap_text(&t, w) {
            out.push(pl(l, Tone::Text, LKind::Note, None));
        }
        // The chips (with counts), one group per line when narrow.
        let quant_on = cv.quant_reported();
        let chip = |label: &str, on: bool, n: Option<usize>| -> String {
            let n = n.map(|n| format!(" {n}")).unwrap_or_default();
            if on {
                format!("[{label}{n}]")
            } else {
                format!("{label}{n}")
            }
        };
        let mut groups: Vec<String> = Vec::new();
        if w < 100 {
            // Narrow: one line, each group's chosen chip with its count
            // (z p t s step through a group's chips).
            let on = |group: &str, value: &str, label: String| -> String {
                if value == "all" {
                    label
                } else {
                    format!("{label} {}", chip_count(cv, f, group, value))
                }
            };
            let quant = QUANT_CHIPS
                .iter()
                .find(|c| c.0 == f.quant)
                .map(|c| c.1)
                .unwrap_or("All");
            let status = STATUS_CHIPS
                .iter()
                .find(|c| c.0 == f.status)
                .map(|c| c.1)
                .unwrap_or("All");
            let prov = if f.provider == "all" {
                "All".to_string()
            } else {
                provider_label(&f.provider)
            };
            groups.push(format!(
                "z Quantization{}: [{}] · p Provider: [{}] · t Capability: [{}] · s Status: [{}]",
                if quant_on { "" } else { " (off)" },
                on("quant", &f.quant, quant.to_string()),
                on("provider", &f.provider, prov),
                on(
                    "cap",
                    &f.cap,
                    if f.cap == "all" {
                        "All".into()
                    } else {
                        cap_label(&f.cap).to_string()
                    }
                ),
                on("status", &f.status, status.to_string()),
            ));
            for g in groups {
                for l in wrap_text(&g, w) {
                    out.push(pl(l, Tone::Faint, LKind::Note, None));
                }
            }
            return notices(cv, st, have, w, out);
        }
        let q: Vec<String> = QUANT_CHIPS
            .iter()
            .map(|(id, l)| {
                chip(
                    l,
                    quant_on && f.quant == *id,
                    (quant_on && *id != "all").then(|| chip_count(cv, f, "quant", id)),
                )
            })
            .collect();
        groups.push(format!(
            "z Quantization{}: {}",
            if quant_on { "" } else { " (off)" },
            q.join(" ")
        ));
        let mut pv = vec![chip("All", f.provider == "all", None)];
        for p in providers(cv, f) {
            pv.push(chip(
                &provider_label(&p),
                f.provider == p,
                Some(chip_count(cv, f, "provider", &p)),
            ));
        }
        groups.push(format!("p Provider: {}", pv.join(" ")));
        let mut cp = vec![chip("All", f.cap == "all", None)];
        for c in caps_offered(cv, f) {
            cp.push(chip(
                cap_label(c),
                f.cap == c,
                Some(chip_count(cv, f, "cap", c)),
            ));
        }
        groups.push(format!("t Capability: {}", cp.join(" ")));
        let sp: Vec<String> = STATUS_CHIPS
            .iter()
            .map(|(id, l)| {
                chip(
                    l,
                    f.status == *id,
                    (*id != "all").then(|| chip_count(cv, f, "status", id)),
                )
            })
            .collect();
        groups.push(format!("s Status: {}", sp.join(" ")));
        for g in groups {
            for l in wrap_text(&g, w) {
                out.push(pl(l, Tone::Faint, LKind::Note, None));
            }
        }
    }
    notices(cv, st, have, w, out)
}

fn notices(cv: &Cv, st: &PageState, have: bool, w: usize, mut out: Vec<PLine>) -> Vec<PLine> {
    let f = &st.filters;
    // Notices: the Hub answered in part; quant_class missing.
    if f.hf_mode() {
        if let Some(hub) = cv.data.and_then(|d| d.get("hub")) {
            if hub.get("ok").and_then(Value::as_bool) == Some(false) {
                for (t, tone) in [
                    (
                        "Hugging Face could not be reached, so these results may be incomplete.",
                        Tone::Warn,
                    ),
                    (
                        "Check that the gateway host is online, then search again.",
                        Tone::Muted,
                    ),
                ] {
                    for l in wrap_text(t, w) {
                        out.push(pl(l, tone, LKind::Note, None));
                    }
                }
            }
        }
    }
    if have && !cv.quant_reported() {
        for (t, tone) in [
            ("This gateway's catalog does not report quant_class yet.", Tone::Warn),
            ("The 4-bit / 8-bit filter needs it, so it is off. Every model and every artifact is still listed below. Updating AbstractCore on the gateway host turns the filter on.", Tone::Muted),
        ] {
            for l in wrap_text(t, w) {
                out.push(pl(l, tone, LKind::Note, None));
            }
        }
    }
    if let Some((tone, t)) = &st.message {
        for l in wrap_text(t, w) {
            out.push(pl(l, *tone, LKind::Note, None));
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

fn admin_of(store: &Store) -> bool {
    !store.conn.with_untracked(ConnPhase::is_known_non_admin)
}

/// Read the catalog, the engines' list, the defaults and the running
/// downloads (`mcLoad` + `uiRestoreDownloads`).
fn load(ctx: &Ctx) {
    let j = ctx.store.json;
    for (k, path, slow) in [
        (K_CATALOG, "/models/catalog", true),
        (K_INSTALLED, "/models/installed", true),
        (K_DEFAULTS, "/config/capability-defaults", false),
        (K_DOWNLOADS, "/models/downloads", false),
    ] {
        if !matches!(j.get_untracked(k), Loadable::Ready(_)) {
            j.set(k, Loadable::Loading);
        }
        ctx.send(Cmd::Json(if slow {
            JsonCmd::get_slow(k, path)
        } else {
            JsonCmd::get(k, path)
        }));
    }
    edit(|p| p.loaded = true);
}

/// `r` (`mcAction("refresh")`): reload, drop the page message, re-run the
/// Hub search in Hugging Face mode.
pub fn refresh(ctx: &Ctx) {
    edit(|p| {
        p.message = None;
        p.extra_notice = None;
    });
    load(ctx);
    let hf = with_state(|p| p.filters.hf.clone());
    if let Some(q) = hf.filter(|q| !q.is_empty()) {
        hub_search(ctx, &q);
    }
}

fn hub_search(ctx: &Ctx, q: &str) {
    let q = q.trim().to_string();
    edit(|p| {
        p.filters.hf = Some(q.clone());
        p.hub_q = Some(q.clone());
    });
    if q.is_empty() {
        return;
    }
    ctx.store.json.set(K_HUB, Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get_slow(
        K_HUB,
        format!("/models/catalog?q={}&hub=true", urlencode(&q)),
    )));
}

#[allow(clippy::too_many_arguments)]
fn send_write(
    ctx: &Ctx,
    key: String,
    method: &str,
    path: String,
    body: Value,
    label: String,
    reload: Vec<(String, String)>,
    journal: bool,
) {
    ctx.store.json.set_write(&key, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key,
        method: method.into(),
        path,
        body,
        slow: true,
        label,
        reload,
        journal,
    }));
}

/// Find an artifact (catalog, Hub answer) or a not-in-catalog row by key.
fn find_art(cv: &Cv, k: &str) -> Option<(Option<Value>, Value)> {
    for d in [cv.data, cv.catalog] {
        for row in rows_of(d) {
            for a in arts(row) {
                if art_key(a) == k {
                    return Some((Some(row.clone()), a.clone()));
                }
            }
        }
    }
    cv.installed_rows()
        .into_iter()
        .find(|r| art_key(r) == k)
        .map(|r| (None, r.clone()))
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

/// The verbs only an admin may use (the footer labels them "admin only"
/// for anyone else, like the web page's disabled buttons).
pub const ADMIN_KEYS: &[&str] = &["w", "d", "u", "c"];

/// The footer's verbs for this page (the admin ones are gated by
/// [`ADMIN_KEYS`] in the footer).
pub fn hints(_non_admin: bool) -> Vec<(&'static str, &'static str)> {
    vec![
        ("↑↓ Enter", "details"),
        ("/", "search"),
        ("w", "download"),
        ("d", "delete"),
        ("u", "use as default"),
        ("c", "cancel download"),
        ("m", "catalog/Hugging Face"),
        ("f", "fits"),
        ("z p t s", "filters"),
        ("x", "clear"),
        ("Y", "copy id"),
        ("r", "refresh"),
    ]
}

fn fg_of(t: &TokenSet, tone: Tone) -> abstracttui::base::Rgba {
    match tone {
        Tone::Text => t.text,
        Tone::Strong => t.text,
        Tone::Muted => t.text_muted,
        Tone::Faint => t.text_faint,
        Tone::Accent => t.accent,
        Tone::Ok => t.ok,
        Tone::Warn => t.warn,
        Tone::Err => t.error,
        Tone::Info => t.info,
    }
}

fn ink(t: &TokenSet, tone: Tone) -> Style {
    let fg = fg_of(t, tone);
    let s = Style::new().fg(fg).bg(t.surface);
    if tone == Tone::Strong {
        s.attrs(Attrs::BOLD)
    } else {
        s
    }
}

struct Snapshot {
    catalog: Loadable<Value>,
    installed: Loadable<Value>,
    defaults: Loadable<Value>,
    hub: Loadable<Value>,
}

fn snapshot(store: &Store, tracked: bool) -> Snapshot {
    let j = store.json;
    let g = |k: &str| {
        if tracked {
            j.get(k)
        } else {
            j.get_untracked(k)
        }
    };
    Snapshot {
        catalog: g(K_CATALOG),
        installed: g(K_INSTALLED),
        defaults: g(K_DEFAULTS),
        hub: g(K_HUB),
    }
}

/// Run `f` with the page's read-only view for the current state.
fn with_cv<R>(snap: &Snapshot, st: &PageState, f: impl FnOnce(&Cv, Option<String>) -> R) -> R {
    let catalog = snap.catalog.ready();
    let hub = snap.hub.ready();
    let data = match &st.filters.hf {
        Some(q) if st.hub_q.as_deref() == Some(q.as_str()) => hub,
        Some(_) => None,
        None => catalog,
    };
    let (installed, installed_err) = match &snap.installed {
        Loadable::Ready(v) if v.get("schema").and_then(Value::as_str) == Some("models_installed_v1") => (Some(v), None),
        Loadable::Ready(v) => (
            None,
            Some(format!(
                "The gateway answered with an unexpected list (schema {}, expected \"models_installed_v1\").",
                v.get("schema").map(|s| s.to_string()).unwrap_or_else(|| "null".into())
            )),
        ),
        Loadable::Failed(e) => (None, Some(err_text(e))),
        _ => (None, None),
    };
    let cv = Cv {
        data,
        catalog,
        installed,
        jobs: &st.jobs,
        deleted: &st.deleted,
    };
    f(&cv, installed_err)
}

/// The page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;
    let rev = cx.signal(0u64);
    let bump = move || rev.update(|r| *r += 1);
    let confirm = InlineConfirm::new(cx);
    // Which artifact the open confirmation is about (`delete` or `cancel`).
    let confirm_key: Signal<Option<String>> = cx.signal(None);

    // Providers' "Browse models" hands an engine over through the shared
    // engine filter: the list opens on that engine's builds (the web's
    // engine card opens the catalog filtered the same way).
    {
        let ef = ctx.screens.store.engine_filter;
        cx.effect(move || {
            let Some(p) = ef.get() else {
                return;
            };
            ef.set(None);
            edit(|st| st.filters.provider = p);
            bump();
        });
    }

    // Read on entry (connected), and again after a reconnect.
    {
        let ctx_l = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            let not_asked = store.json.slots.with(|m| !m.contains_key(K_CATALOG));
            if !connected || !not_asked {
                return;
            }
            if with_state(|p| p.loaded) {
                // A reconnect: the previous gateway's page state goes too.
                let filters = with_state(|p| p.filters.clone());
                reset_state();
                edit(|p| {
                    p.filters = Filters {
                        hf: None,
                        ..filters
                    }
                });
            }
            load(&ctx_l);
            bump();
        });
    }

    // Running downloads found on open are followed (uiRestoreDownloads).
    {
        let ctx_d = ctx.clone();
        cx.effect(move || {
            let Loadable::Ready(d) = store.json.get(K_DOWNLOADS) else {
                return;
            };
            store.json.set(K_DOWNLOADS, Loadable::NotAsked);
            let mut changed = false;
            for job in d
                .get("jobs")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if job_active(job) && !job_id(job).is_empty() {
                    apply_job(job);
                    changed = true;
                }
            }
            if changed {
                poll_jobs(&ctx_d);
                bump();
            }
        });
    }

    // Polled jobs land here.
    {
        let ctx_j = ctx.clone();
        cx.effect(move || {
            let polled: Vec<(String, Loadable<Value>)> = store.json.slots.with(|m| {
                m.iter()
                    .filter(|(k, v)| {
                        k.starts_with(K_JOB) && !matches!(v, Loadable::Loading | Loadable::NotAsked)
                    })
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            });
            if polled.is_empty() {
                return;
            }
            let mut finished = false;
            for (k, v) in polled {
                store.json.set(&k, Loadable::NotAsked);
                match v {
                    Loadable::Ready(body) => {
                        if let Some(job) = body.get("job") {
                            let was = with_state(|p| {
                                p.jobs
                                    .values()
                                    .any(|j| job_id(j) == job_id(job) && job_active(j))
                            });
                            apply_job(job);
                            if was && !job_active(job) {
                                finished = true;
                                edit(|p| {
                                    p.cancelling.remove(&job_id(job));
                                });
                            }
                        }
                    }
                    Loadable::Failed(_) => {
                        // 404 after a gateway restart: the weights may have
                        // landed; re-read rather than report a failure.
                        let id = k.trim_start_matches(K_JOB).to_string();
                        edit(|p| p.jobs.retain(|_, j| job_id(j) != id));
                        finished = true;
                    }
                    _ => {}
                }
            }
            if finished {
                load(&ctx_j);
            }
            bump();
        });
    }

    // A poll while any job runs (the web's 1.5 s fallback poll).
    {
        let ctx_p = ctx.clone();
        let _h = abstracttui::reactive::interval(cx, JOB_POLL, move || poll_jobs(&ctx_p));
    }

    // Write outcomes.
    {
        let ctx_w = ctx.clone();
        cx.effect(move || {
            let done: Vec<(String, WriteState)> = store.json.writes.with(|m| {
                m.iter()
                    .filter(|(k, v)| k.starts_with("catalog.") && !v.is_pending())
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            });
            if done.is_empty() {
                return;
            }
            for (k, w) in done {
                store.json.set_write(&k, None);
                on_write(&ctx_w, &k, w, confirm, confirm_key);
            }
            bump();
        });
    }

    // The search box: rebuilt with the keyboard when `/` asks for it
    // (Esc gives the keys back to the page).
    let search = cx.signal(with_state(|p| {
        p.filters.hf.clone().unwrap_or_else(|| p.filters.q.clone())
    }));
    let search_gen = cx.signal(0u64);
    // The list's node, recorded as events pass through it: Esc in the
    // search box hands the keyboard back to the LIST (the page's keys live
    // on its path), not to the tree root.
    let list_id: std::rc::Rc<std::cell::Cell<Option<abstracttui::ui::ViewId>>> =
        std::rc::Rc::new(std::cell::Cell::new(None));
    let list_id_esc = list_id.clone();
    let ctx_s = ctx.clone();
    let search_box = dyn_view_scoped(LayoutStyle::default().grow(1.0).h(1), move |gcx| {
        let g = search_gen.get();
        let hf = with_state(|p| p.filters.hf_mode());
        let ctx_s = ctx_s.clone();
        let caret_c = ctx_s.ui.caret;
        let el = TextInput::new()
            .value(search)
            .placeholder(if hf {
                "Search Hugging Face, then press Enter"
            } else {
                "Search by model, organisation or artifact id"
            })
            .layout(LayoutStyle::default().grow(1.0).h(1))
            .on_change(move |text: &str| {
                if !with_state(|p| p.filters.hf_mode()) {
                    let text = text.to_string();
                    edit(|p| p.filters.q = text);
                    bump();
                }
            })
            .on_submit(move |text: &str| {
                if with_state(|p| p.filters.hf_mode()) {
                    hub_search(&ctx_s, text);
                    bump();
                }
            })
            .element(gcx, &tt);
        let el = super::w::caret_tracked(gcx, caret_c, el);
        let list_id_esc = list_id_esc.clone();
        let el = el.shortcut(KeyChord::plain(Key::Escape), move |ecx| {
            let target = list_id_esc.get().or_else(|| ecx.current());
            if let Some(id) = target {
                ecx.request_focus(id);
                store.notice.set(Some(super::util::FOCUS_RELEASED.into()));
            }
        });
        if g > 0 {
            el.autofocus().build()
        } else {
            el.build()
        }
    });

    let vp = crate::ui::page_viewport(cx);

    // The list (focusable, keys) + its reactive painter.
    let list_focus = cx.signal(false);
    let top = std::rc::Rc::new(std::cell::Cell::new(0i32));
    let painted: std::rc::Rc<RefCell<Vec<Option<String>>>> =
        std::rc::Rc::new(RefCell::new(Vec::new()));
    let painted_ev = painted.clone();
    let page_h = std::rc::Rc::new(std::cell::Cell::new(10i32));
    let page_h_ev = page_h.clone();
    let list = Element::new()
        .style(LayoutStyle::default().grow(1.0).min_h(3))
        .focusable()
        .autofocus()
        .focus_signal(list_focus)
        .on(Phase::Capture, move |ectx, _| {
            if list_id.get().is_none() {
                list_id.set(ectx.current());
            }
        })
        .on(Phase::Bubble, move |ectx, ev| match ev {
            UiEvent::Key(k) if k.mods.0 == 0 => {
                let order: Vec<String> = {
                    let p = painted_ev.borrow();
                    let mut o: Vec<String> = Vec::new();
                    for k in p.iter().flatten() {
                        if o.last() != Some(k) && !o.contains(k) {
                            o.push(k.clone());
                        }
                    }
                    o
                };
                let all = with_state(|p| p.sel.clone());
                let _ = all;
                let sel_list = SEL_ORDER.with(|s| s.borrow().clone());
                let order = if sel_list.is_empty() { order } else { sel_list };
                let cur = with_state(|p| p.sel.clone())
                    .and_then(|s| order.iter().position(|x| *x == s))
                    .unwrap_or(0);
                let n = order.len();
                let pageh = (page_h_ev.get() / 2).max(1) as usize;
                let target = match k.key {
                    Key::Up | Key::Char('k') => Some(cur.saturating_sub(1)),
                    Key::Down | Key::Char('j') => Some((cur + 1).min(n.saturating_sub(1))),
                    Key::PageUp => Some(cur.saturating_sub(pageh)),
                    Key::PageDown => Some((cur + pageh).min(n.saturating_sub(1))),
                    Key::Home => Some(0),
                    Key::End => Some(n.saturating_sub(1)),
                    Key::Enter => {
                        if let Some(s) = order.get(cur).cloned() {
                            edit(|p| {
                                p.expanded = if p.expanded.as_deref() == Some(s.as_str()) {
                                    None
                                } else {
                                    Some(s.clone())
                                };
                                p.sel = Some(s);
                            });
                            bump();
                        }
                        ectx.stop_propagation();
                        None
                    }
                    _ => None,
                };
                if let Some(i) = target {
                    if let Some(s) = order.get(i).cloned() {
                        edit(|p| p.sel = Some(s));
                        bump();
                    }
                    ectx.stop_propagation();
                }
            }
            UiEvent::Mouse(m) => match m.kind {
                MouseKind::Down(MouseButton::Left) => {
                    let rect = ectx.current_rect();
                    let y = m.pos.y - rect.y;
                    if y >= 0 {
                        if let Some(Some(k)) = painted_ev.borrow().get(y as usize) {
                            let k = k.clone();
                            edit(|p| p.sel = Some(k));
                            bump();
                        }
                    }
                    ectx.stop_propagation();
                }
                MouseKind::ScrollDown | MouseKind::ScrollUp => {
                    let order = SEL_ORDER.with(|s| s.borrow().clone());
                    let cur = with_state(|p| p.sel.clone())
                        .and_then(|s| order.iter().position(|x| *x == s))
                        .unwrap_or(0);
                    let i = if matches!(m.kind, MouseKind::ScrollDown) {
                        (cur + 1).min(order.len().saturating_sub(1))
                    } else {
                        cur.saturating_sub(1)
                    };
                    if let Some(s) = order.get(i).cloned() {
                        edit(|p| p.sel = Some(s));
                        bump();
                    }
                    ectx.stop_propagation();
                }
                _ => {}
            },
            _ => {}
        })
        .child(dyn_view(
            LayoutStyle::default().grow(1.0).min_h(1),
            move || {
                let _ = rev.get();
                let focus = list_focus.get();
                let snap = snapshot(&store, true);
                let admin = !store.conn.with(ConnPhase::is_known_non_admin);
                let pending = confirm.pending.get();
                let ck = confirm_key.get();
                let top = top.clone();
                let painted = painted.clone();
                let page_h = page_h.clone();
                Element::new()
                    .style(LayoutStyle::default().grow(1.0).min_h(1))
                    .draw(move |canvas, rect| {
                        if rect.is_empty() {
                            return;
                        }
                        let ground = Style::new().fg(tt.text).bg(tt.surface);
                        canvas.fill_styled(rect, ' ', &ground);
                        let lines = with_state(|st| {
                            with_cv(&snap, st, |cv, ierr| {
                                let default = current_default(snap.defaults.ready());
                                let conf = match (&pending, &ck) {
                                    (Some(p), Some(k)) => {
                                        Some((k.clone(), p.sentence.clone(), p.confirm.clone()))
                                    }
                                    _ => None,
                                };
                                list_lines(
                                    cv,
                                    st,
                                    &snap.hub,
                                    &snap.catalog,
                                    ierr,
                                    &default,
                                    admin,
                                    conf,
                                    rect.w,
                                )
                            })
                        });
                        let order = selectable(&lines);
                        SEL_ORDER.with(|s| *s.borrow_mut() = order.clone());
                        // Keep a valid selection.
                        let sel = with_state(|p| p.sel.clone())
                            .filter(|s| order.contains(s))
                            .or_else(|| order.first().cloned());
                        if with_state(|p| p.sel != sel) {
                            edit(|p| p.sel = sel.clone());
                        }
                        let first = lines
                            .iter()
                            .position(|l| l.row == sel && l.row.is_some())
                            .unwrap_or(0) as i32;
                        let last = lines
                            .iter()
                            .rposition(|l| l.row == sel && l.row.is_some())
                            .unwrap_or(0) as i32;
                        let h = rect.h.max(1);
                        page_h.set(h);
                        let mut tp = top.get();
                        if first < tp {
                            tp = first;
                        }
                        if last >= tp + h {
                            tp = (last - h + 1).min(first);
                        }
                        tp = tp.clamp(0, (lines.len() as i32 - h).max(0));
                        top.set(tp);
                        let mut map = Vec::new();
                        for (y, l) in (rect.y..).zip(lines.iter().skip(tp as usize)) {
                            if y >= rect.y + rect.h {
                                break;
                            }
                            let selected = l.kind == LKind::Art && l.row.is_some() && l.row == sel;
                            let mut x = rect.x;
                            if selected {
                                let st = if focus {
                                    Style::new().fg(tt.selection_fg).bg(tt.selection_bg)
                                } else {
                                    Style::new()
                                        .fg(tt.text)
                                        .bg(tt.surface_raised)
                                        .attrs(Attrs::BOLD)
                                };
                                canvas.fill_styled(
                                    abstracttui::base::Rect::new(rect.x, y, rect.w, 1),
                                    ' ',
                                    &st,
                                );
                                let text = l.text();
                                canvas.print_styled(Point::new(x, y), &clip(&text, rect.w), &st);
                            } else {
                                for (s, tone) in &l.spans {
                                    let room = rect.x + rect.w - x;
                                    if room <= 0 {
                                        break;
                                    }
                                    x += canvas.print_styled(
                                        Point::new(x, y),
                                        &clip(s, room),
                                        &ink(&tt, *tone),
                                    );
                                }
                            }
                            map.push(l.row.clone());
                        }
                        *painted.borrow_mut() = map;
                    })
                    .build()
            },
        ));

    // Page keys.
    let mut root = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let chord = |c: char| KeyChord::plain(Key::Char(c));
    {
        let c = ctx.clone();
        root = root.shortcut(chord('r'), move |_| {
            refresh(&c);
            c.store.notice.set(Some("⟳ refreshing Models…".into()));
            bump();
        });
    }
    root = root.shortcut(chord('/'), move |_| search_gen.update(|g| *g += 1));
    {
        let c = ctx.clone();
        root = root.shortcut(chord('m'), move |_| {
            let hf = with_state(|p| p.filters.hf_mode());
            if hf {
                edit(|p| p.filters.hf = None);
                search.set(with_state(|p| p.filters.q.clone()));
            } else {
                // The catalog query moves to the Hub search (mcSetMode).
                let q = with_state(|p| p.filters.q.clone());
                edit(|p| p.filters.q.clear());
                hub_search(&c, &q);
                search.set(q);
            }
            bump();
        });
    }
    root = root.shortcut(chord('f'), move |_| {
        edit(|p| p.filters.fits = !p.filters.fits);
        bump();
    });
    root = root.shortcut(chord('x'), move |_| {
        edit(|p| {
            let hf = p.filters.hf.clone();
            p.filters = Filters {
                hf,
                ..Filters::default()
            };
        });
        search.set(with_state(|p| p.filters.hf.clone().unwrap_or_default()));
        bump();
    });
    for (ch, group) in [
        ('z', "quant"),
        ('p', "provider"),
        ('t', "cap"),
        ('s', "status"),
    ] {
        root = root.shortcut(chord(ch), move |_| {
            let snap = snapshot(&store, false);
            let next = with_state(|st| {
                with_cv(&snap, st, |cv, _| {
                    let f = &st.filters;
                    let (opts, cur): (Vec<String>, String) = match group {
                        "quant" => {
                            if !cv.quant_reported() {
                                return None;
                            }
                            (QUANT_CHIPS.iter().map(|c| c.0.to_string()).collect(), f.quant.clone())
                        }
                        "provider" => {
                            let mut o = vec!["all".to_string()];
                            o.extend(providers(cv, f));
                            (o, f.provider.clone())
                        }
                        "cap" => {
                            let mut o = vec!["all".to_string()];
                            o.extend(caps_offered(cv, f).into_iter().map(str::to_string));
                            (o, f.cap.clone())
                        }
                        _ => (STATUS_CHIPS.iter().map(|c| c.0.to_string()).collect(), f.status.clone()),
                    };
                    let i = opts.iter().position(|o| *o == cur).unwrap_or(0);
                    opts.get((i + 1) % opts.len().max(1)).cloned()
                })
            });
            match next {
                Some(v) => {
                    edit(|p| match group {
                        "quant" => p.filters.quant = v,
                        "provider" => p.filters.provider = v,
                        "cap" => p.filters.cap = v,
                        _ => p.filters.status = v,
                    });
                    bump();
                }
                None => store.notice.set(Some(
                    "This gateway's catalog does not report quant_class yet: the 4-bit / 8-bit filter is off.".into(),
                )),
            }
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(chord('w'), move |_| {
            act(&c, Act::Download, confirm, confirm_key);
            bump();
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(chord('d'), move |_| {
            act(&c, Act::Delete, confirm, confirm_key);
            bump();
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(chord('u'), move |_| {
            act(&c, Act::Default, confirm, confirm_key);
            bump();
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(chord('c'), move |_| {
            act(&c, Act::Cancel, confirm, confirm_key);
            bump();
        });
    }
    root = root.shortcut(chord('Y'), move |_| {
        if let Some(k) = with_state(|p| p.sel.clone()) {
            let snap = snapshot(&store, false);
            let art = with_state(|st| with_cv(&snap, st, |cv, _| find_art(cv, &k)));
            if let Some((_, a)) = art {
                let id = sv(&a, "artifact").to_string();
                copy_to_clipboard(id.clone());
                store.notice.set(Some(format!("copied {id}")));
            }
        }
    });
    let root = confirm.keys(root);

    // The host line above the bar; the count, chips and notices below it.
    let host = dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
        let _ = rev.get();
        let snap = snapshot(&store, true);
        let w = vp.get().w - 4;
        let loading = snap.catalog.is_loading() || snap.installed.is_loading();
        let lines = with_state(|st| with_cv(&snap, st, |cv, _| host_lines(cv, loading, w)));
        lines_view(&tt, lines)
    });
    let header = dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
        let _ = rev.get();
        let snap = snapshot(&store, true);
        let w = vp.get().w - 4;
        let lines = with_state(|st| with_cv(&snap, st, |cv, _| header_lines(cv, st, w)));
        lines_view(&tt, lines)
    });
    // The bar: mode, fits, then the search box (it takes the rest).
    let bar_left = dyn_view(LayoutStyle::row().gap(0).shrink(0.0), move || {
        let _ = rev.get();
        let (hf, fits) = with_state(|p| (p.filters.hf_mode(), p.filters.fits));
        let mode = if hf {
            "m Catalog [Hugging Face]"
        } else {
            "m [Catalog] Hugging Face"
        };
        line(vec![
            span(mode, tt.text_muted),
            span("  ·  ", tt.text_faint),
            span(
                format!(
                    "{} Fits this computer (f)",
                    super::switch::marker(fits, false)
                ),
                if fits { tt.accent } else { tt.text_muted },
            ),
            span("  ·  ", tt.text_faint),
            span(
                if hf {
                    "/ Search Hugging Face: "
                } else {
                    "/ Search: "
                },
                tt.text_muted,
            ),
        ])
    });
    let bar = Element::new()
        .style(LayoutStyle::row().gap(0).h(1).shrink(0.0))
        .child(bar_left)
        .child(search_box)
        .build();

    // n / Esc on a delete confirmation: back to the trash verb.
    cx.effect(move || {
        let open = confirm.pending.with(Option::is_some);
        if open {
            return;
        }
        if let Some(k) = confirm_key.get_untracked() {
            edit(|p| {
                if matches!(p.del.get(&k), Some(DelPhase::Confirm(_))) {
                    p.del.remove(&k);
                }
            });
            confirm_key.set(None);
            bump();
        }
    });

    root.child(
        Block::new()
            .border(BorderKind::Rounded)
            .title("Models — browse, download and delete models that fit this machine")
            .fill(t.surface)
            .layout(
                LayoutStyle::column()
                    .gap(0)
                    .grow(1.0)
                    .padding(Edges::hv(1, 0))
                    .clip(),
            )
            .child(host)
            .child(bar)
            .child(header)
            .child(list)
            .element(t)
            .build(),
    )
    .build()
}

fn lines_view(tt: &TokenSet, lines: Vec<PLine>) -> View {
    let views: Vec<View> = lines
        .into_iter()
        .map(|l| {
            line(
                l.spans
                    .into_iter()
                    .map(|(s, tone)| match tone {
                        Tone::Strong => span_bold(s, tt.text),
                        other => span(s, fg_of(tt, other)),
                    })
                    .collect(),
            )
        })
        .collect();
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .children(views)
        .build()
}

thread_local! {
    static SEL_ORDER: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
}

fn clip(s: &str, w: i32) -> String {
    if abstracttui::text::width(s) <= w {
        return s.to_string();
    }
    let mut out = String::new();
    let mut used = 0;
    for ch in s.chars() {
        let cw = abstracttui::text::width(&ch.to_string());
        if used + cw > w {
            break;
        }
        out.push(ch);
        used += cw;
    }
    out
}

/// Store one job by provider/artifact (`dlApply`): an older finished job
/// never hides the live one.
fn apply_job(job: &Value) {
    let k = key(sv(job, "provider"), sv(job, "artifact"));
    edit(|p| {
        if let Some(known) = p.jobs.get(&k) {
            if job_id(known) != job_id(job) && job_active(known) && !job_active(job) {
                return;
            }
        }
        p.jobs.insert(k, job.clone());
    });
}

/// One `GET /models/download/{id}` per active job.
fn poll_jobs(ctx: &Ctx) {
    let ids: Vec<String> = with_state(|p| {
        p.jobs
            .values()
            .filter(|j| job_active(j))
            .map(job_id)
            .filter(|i| !i.is_empty())
            .collect()
    });
    for id in ids {
        let k = format!("{K_JOB}{id}");
        if matches!(ctx.store.json.get_untracked(&k), Loadable::Loading) {
            continue;
        }
        ctx.store.json.set(&k, Loadable::Loading);
        ctx.send(Cmd::Json(JsonCmd::get(
            &k,
            format!("/models/download/{}", urlencode(&id)),
        )));
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Act {
    Download,
    Delete,
    Default,
    Cancel,
}

fn act(ctx: &Ctx, what: Act, confirm: InlineConfirm, confirm_key: Signal<Option<String>>) {
    let store = ctx.store;
    let Some(k) = with_state(|p| p.sel.clone()) else {
        store.notice.set(Some("No model row selected.".into()));
        return;
    };
    let snap = snapshot(&store, false);
    let found = with_state(|st| {
        with_cv(&snap, st, |cv, _| {
            find_art(cv, &k).map(|(row, a)| {
                let installed = cv.installed(&a) || row.is_none();
                let job = cv.jobs.get(&k).cloned();
                (row, a, installed, job)
            })
        })
    });
    let Some((row, a, installed, job)) = found else {
        return;
    };
    let admin = admin_of(&store);
    let (provider, artifact) = (
        sv(&a, "provider").to_string(),
        sv(&a, "artifact").to_string(),
    );
    match what {
        Act::Download => {
            if !admin {
                store
                    .notice
                    .set(Some("Only an admin can download models".into()));
                return;
            }
            if job.as_ref().is_some_and(job_active) || installed {
                return;
            }
            if a.get("downloadable").and_then(Value::as_bool) != Some(true) {
                let why = if a.get("supported_on_host").and_then(Value::as_bool) == Some(false) {
                    "Its engine does not run on this computer"
                } else {
                    "This build cannot be downloaded from here"
                };
                store.notice.set(Some(format!("Not available here: {why}")));
                return;
            }
            let mut body = json!({"provider": provider, "artifact": artifact});
            if let Some(b) = num(&a, "download_bytes") {
                if matches!(sv(&a, "size_source"), "catalog" | "hf_api") {
                    body["expected_bytes"] = json!(b as u64);
                }
            }
            edit(|p| {
                p.busy.insert(k.clone());
                p.notices.remove(&k);
                p.deleted.remove(&k);
            });
            send_write(
                ctx,
                format!("{W_DOWNLOAD}{k}"),
                "POST",
                "/models/download".into(),
                body,
                format!("Download {artifact}"),
                vec![],
                true,
            );
        }
        Act::Delete => {
            if !admin {
                store
                    .notice
                    .set(Some("Only an admin can delete downloaded models".into()));
                return;
            }
            if !installed || with_state(|p| p.del.contains_key(&k)) {
                return;
            }
            edit(|p| {
                p.notices.remove(&k);
                p.extra_notice = None;
                p.del.insert(k.clone(), DelPhase::Checking);
            });
            send_write(
                ctx,
                format!("{W_PLAN}{k}"),
                "POST",
                DELETE_URL.into(),
                json!({"provider": provider, "artifact": artifact, "dry_run": true}),
                format!("Check delete {artifact}"),
                vec![],
                false,
            );
        }
        Act::Default => {
            if !admin {
                store
                    .notice
                    .set(Some("Only an admin can change the default model".into()));
                return;
            }
            let Some(row) = row else { return };
            if !installed || !can_be_default(&row) {
                return;
            }
            let model = served_model_id(&provider, &artifact);
            send_write(
                ctx,
                W_DEFAULT.into(),
                "PUT",
                "/config/capability-defaults/output/text".into(),
                json!({"provider": provider, "model": model, "base_url": "", "reasoning": "", "options": {}}),
                format!("Use {provider} · {model} as the default text model"),
                vec![(K_DEFAULTS.into(), "/config/capability-defaults".into())],
                true,
            );
        }
        Act::Cancel => {
            let Some(j) = job.filter(job_active) else {
                return;
            };
            let id = job_id(&j);
            let c = ctx.clone();
            confirm_key.set(Some(k.clone()));
            confirm.ask("Stop this download?", "Stop download", move || {
                edit(|p| {
                    p.cancelling.insert(id.clone());
                });
                send_write(
                    &c,
                    format!("{W_CANCEL}{id}"),
                    "POST",
                    format!("/models/download/{}/cancel", urlencode(&id)),
                    json!({"via": "console"}),
                    format!("Cancel download {id}"),
                    vec![],
                    true,
                );
            });
        }
    }
}

fn on_write(
    ctx: &Ctx,
    k: &str,
    w: WriteState,
    confirm: InlineConfirm,
    confirm_key: Signal<Option<String>>,
) {
    let snap = snapshot(&ctx.store, false);
    if let Some(art) = k.strip_prefix(W_DOWNLOAD) {
        edit(|p| {
            p.busy.remove(art);
        });
        match w {
            WriteState::Done(v) => match v.get("job") {
                Some(job) => {
                    apply_job(job);
                    poll_jobs(ctx);
                }
                None => edit(|p| {
                    p.notices.insert(
                        art.to_string(),
                        (Tone::Err, "Could not start the download: The gateway accepted the download but returned no job to follow.".into()),
                    );
                }),
            },
            WriteState::Failed(e) => edit(|p| {
                p.notices.insert(
                    art.to_string(),
                    (
                        Tone::Err,
                        format!("Could not start the download: {}", err_text(&e)),
                    ),
                );
            }),
            WriteState::Pending => {}
        }
    } else if let Some(id) = k.strip_prefix(W_CANCEL) {
        match w {
            WriteState::Done(v) => {
                if let Some(job) = v.get("job") {
                    apply_job(job);
                }
            }
            WriteState::Failed(e) => {
                edit(|p| {
                    p.cancelling.remove(id);
                });
                ctx.store
                    .notice
                    .set(Some(format!("Could not cancel: {}", err_text(&e))));
            }
            WriteState::Pending => {}
        }
    } else if let Some(art) = k.strip_prefix(W_PLAN) {
        match w {
            WriteState::Done(plan) => {
                edit(|p| {
                    p.del
                        .insert(art.to_string(), DelPhase::Confirm(plan.clone()));
                });
                let sentence = confirm_sentence(&plan);
                let c = ctx.clone();
                let key_s = art.to_string();
                confirm_key.set(Some(key_s.clone()));
                let found = with_state(|st| with_cv(&snap, st, |cv, _| find_art(cv, &key_s)));
                let Some((_, a)) = found else { return };
                let (provider, artifact) = (
                    sv(&a, "provider").to_string(),
                    sv(&a, "artifact").to_string(),
                );
                confirm.ask(sentence, "Delete", move || {
                    edit(|p| {
                        p.del.insert(key_s.clone(), DelPhase::Deleting);
                    });
                    send_write(
                        &c,
                        format!("{W_DELETE}{key_s}"),
                        "POST",
                        DELETE_URL.into(),
                        json!({"provider": provider, "artifact": artifact, "dry_run": false}),
                        format!("Delete {artifact}"),
                        vec![],
                        true,
                    );
                });
            }
            WriteState::Failed(e) => {
                let gone = e
                    .body
                    .as_ref()
                    .map(|b| sv(b, "reason") == "not_downloaded")
                    .unwrap_or(false);
                let extra = with_state(|st| with_cv(&snap, st, |cv, _| cv.is_extra_key(art)));
                let (tone, text) = delete_notice(&e);
                let artifact = art.split_once('/').map(|x| x.1).unwrap_or(art).to_string();
                edit(|p| {
                    p.del.remove(art);
                    if gone && extra {
                        p.extra_notice = Some((tone, format!("{artifact}: {text}")));
                    } else {
                        p.notices.insert(art.to_string(), (tone, text));
                    }
                    if gone {
                        p.deleted.insert(art.to_string());
                    }
                });
            }
            WriteState::Pending => {}
        }
    } else if let Some(art) = k.strip_prefix(W_DELETE) {
        let artifact = art.split_once('/').map(|x| x.1).unwrap_or(art).to_string();
        match w {
            WriteState::Done(res) => {
                let extra = with_state(|st| with_cv(&snap, st, |cv, _| cv.is_extra_key(art)));
                let freed = num(&res, "freed_bytes")
                    .map(|b| format!(" {} freed.", ui_bytes(b)))
                    .unwrap_or_default();
                // The row goes away: the selection moves to its neighbour so
                // the page stays where the person is (and shows the notice).
                let neighbour = SEL_ORDER.with(|o| {
                    let o = o.borrow();
                    let i = o.iter().position(|k| k == art)?;
                    o.get(i + 1)
                        .or_else(|| i.checked_sub(1).and_then(|j| o.get(j)))
                        .cloned()
                });
                edit(|p| {
                    p.del.remove(art);
                    p.deleted.insert(art.to_string());
                    if extra && p.sel.as_deref() == Some(art) {
                        p.sel = neighbour.clone();
                    }
                    if extra {
                        p.extra_notice = Some((Tone::Ok, format!("Deleted {artifact}.{freed}")));
                    } else {
                        p.notices.insert(
                            art.to_string(),
                            (Tone::Ok, format!("Download deleted.{freed}")),
                        );
                    }
                });
                load(ctx);
            }
            WriteState::Failed(e) => {
                edit(|p| {
                    p.del.remove(art);
                    p.notices.insert(art.to_string(), delete_notice(&e));
                });
            }
            WriteState::Pending => {}
        }
    } else if k == W_DEFAULT {
        match w {
            WriteState::Done(_) => {
                let d = ctx.store.json.get_untracked(K_DEFAULTS);
                let cur = current_default(d.ready());
                let text = match cur {
                    Some((p, m)) => format!("Default text model: {} · {m}.", provider_label(&p)),
                    None => "Default text model saved.".into(),
                };
                edit(|p| p.message = Some((Tone::Ok, text)));
                // The Multimodal screen reads the routes again on its next visit.
                ctx.store.routes.set(Loadable::NotAsked);
            }
            WriteState::Failed(e) => edit(|p| {
                p.message = Some((
                    Tone::Err,
                    format!("Could not set the default: {}", err_text(&e)),
                ));
            }),
            WriteState::Pending => {}
        }
    }
}
