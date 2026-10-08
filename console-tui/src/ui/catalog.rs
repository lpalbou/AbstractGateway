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
//! R15 (DESIGN-TUI.md §3.9): the list is a `w::DataTable` — one group line
//! per model above its artifact rows (Artifact · Provider · Quant · Size ·
//! Weights · Fit · Actions; narrow: Provider/Quant/Size/Fit under the id),
//! the rows outside the catalog under "Not in the catalog". Each row's
//! actions come from ONE `row_actions` (labelled Download / Try again / Use
//! as default / Cancel, the ⌫ trash glyph with the web's tooltip; refused
//! ones faint with the web's reason). The head: [Check again]; the bar:
//! Catalog | Hugging Face (Segmented), the search field, [Search] in Hugging
//! Face mode, "Fits this computer" (Toggle); the four chip groups are
//! Selects. Delete and Cancel confirm with `w::Confirm` ([Delete] [Keep],
//! [Stop download] [Keep downloading]). Enter = the row's first action;
//! `i` opens its facts.
//!
//! The page's own state (filters, selection, the delete in progress…)
//! lives in a thread-local so it survives a tab switch like the web
//! page's `mcStore`; a reconnect (the catalog slot back to NotAsked)
//! starts it afresh.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use abstracttui::widgets::TextInput;
use serde_json::{json, Value};

use super::w::action::{button, On};
use super::w::{Action, Cell, Col, ColW, Confirm, DataTable, Row, Segmented, Toggle};
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
// R15: the artifact rows as DataTable rows, their actions as `w::Action`s
// (ONE source for the Actions cell, the keys, the hint bar and the tests)
// ---------------------------------------------------------------------------

/// The web's delete tooltip (`mcDeleteButton`).
pub fn delete_tip(artifact: &str) -> String {
    format!(
        "Delete {} from this computer (files only)",
        if artifact.is_empty() {
            "this model"
        } else {
            artifact
        }
    )
}

/// The trash button of a downloaded row (`mcDeleteButton`): a glyph, its
/// tooltip the web's sentence; while the dry run / the delete runs it is
/// faint with what it is doing.
fn delete_action(st: &PageState, k: &str, artifact: &str, admin: bool) -> Action {
    let a = Action::glyph("delete", "Delete")
        .key('d')
        .tooltip(delete_tip(artifact))
        .danger();
    match st.del.get(k) {
        Some(DelPhase::Checking) | Some(DelPhase::Confirm(_)) => {
            a.refused(Some("Checking what a delete would free".into()))
        }
        Some(DelPhase::Deleting) => a.refused(Some("Deleting the downloaded files".into())),
        None if !admin => a.refused(Some("Only an admin can delete downloaded models".into())),
        None => a,
    }
}

/// Is `a` the current default text model?
fn is_default(a: &Value, default: &Option<(String, String)>) -> bool {
    let model = served_model_id(sv(a, "provider"), sv(a, "artifact"));
    default
        .as_ref()
        .is_some_and(|(p, m)| p == sv(a, "provider") && *m == model)
}

/// One artifact row's actions, in the web's order (`mcActionMarkup`):
/// a running download → Cancel; downloaded → Use as default (text models,
/// not the current default) + the trash; downloadable → Download (or Try
/// again after a failure); otherwise "Not available here" with its reason.
/// `row` is None for a row outside the catalog (trash only). An action
/// that cannot run here stays, faint, with the web's reason.
pub fn row_actions(
    cv: &Cv,
    st: &PageState,
    row: Option<&Value>,
    a: &Value,
    admin: bool,
    default: &Option<(String, String)>,
) -> Vec<Action> {
    let k = art_key(a);
    let artifact = sv(a, "artifact");
    if let Some(j) = cv.job(a).filter(|j| job_active(j)) {
        if st.cancelling.contains(&job_id(j)) {
            return vec![Action::label("cancelling", "Cancelling...")
                .refused(Some("Stopping the download".into()))];
        }
        return vec![Action::label("cancel", "Cancel").key('c')];
    }
    if st.busy.contains(&k) {
        return vec![
            Action::label("starting", "Starting...").refused(Some("Starting the download".into()))
        ];
    }
    let installed = cv.installed(a) || row.is_none();
    if installed {
        let del = delete_action(st, &k, artifact, admin);
        let Some(row) = row else { return vec![del] };
        if !can_be_default(row) || is_default(a, default) {
            return vec![del];
        }
        let use_default = Action::label("default", "Use as default")
            .key('u')
            .refused((!admin).then(|| "Only an admin can change the default model".into()));
        return vec![use_default, del];
    }
    if a.get("downloadable").and_then(Value::as_bool) != Some(true) {
        let why = if a.get("supported_on_host").and_then(Value::as_bool) == Some(false) {
            "Its engine does not run on this computer"
        } else {
            "This build cannot be downloaded from here"
        };
        return vec![Action::label("unavailable", "Not available here").refused(Some(why.into()))];
    }
    let failed = cv.job(a).is_some_and(|j| sv(j, "status") == "failed");
    let label = if failed { "Try again" } else { "Download" };
    vec![Action::label("download", label)
        .key('w')
        .refused((!admin).then(|| "Only an admin can download models".into()))]
}

/// Every visible artifact row's actions (key, actions) for the current
/// filters — what the page offers; the click tests' meta-test reads it.
pub fn offered_actions(
    catalog: &Value,
    installed: &Value,
    defaults: &Value,
    admin: bool,
) -> Vec<(String, Vec<Action>)> {
    let st = PageState::default();
    let cv = Cv {
        data: Some(catalog),
        catalog: Some(catalog),
        installed: Some(installed),
        jobs: &st.jobs,
        deleted: &st.deleted,
    };
    let default = current_default(Some(defaults));
    let mut out = Vec::new();
    for item in visible(&cv, &st.filters) {
        for a in &item.arts {
            out.push((
                art_key(a),
                row_actions(&cv, &st, Some(item.row), a, admin, &default),
            ));
        }
    }
    for r in extras(&cv, &st.filters) {
        out.push((art_key(r), row_actions(&cv, &st, None, r, admin, &default)));
    }
    out
}

/// The facts of one artifact (engine · quant · size · weights · fit).
struct ArtFacts {
    provider: String,
    quant: String,
    size: String,
    weights: (String, Tone),
    fit: (String, Tone),
}

fn art_facts(cv: &Cv, st: &PageState, a: &Value, alone: bool) -> ArtFacts {
    let k = art_key(a);
    let job = cv.job(a);
    let installed = cv.installed(a);
    let gone = st.deleted.contains(&k);
    let presence = a.get("presence").map(|p| sv(p, "status")).unwrap_or("");
    let weights = match job {
        Some(j) if job_active(j) => {
            if st.cancelling.contains(&job_id(j)) {
                ("Cancelling".to_string(), Tone::Warn)
            } else {
                let phase = job_phase(j);
                let (l, t) = state_pill(&phase);
                (l.to_string(), t)
            }
        }
        _ => {
            let (l, t) = if installed {
                weights_label("installed")
            } else if gone {
                weights_label("absent")
            } else {
                weights_label(presence)
            };
            (l.to_string(), t)
        }
    };
    let fit = if a.get("supported_on_host").and_then(Value::as_bool) == Some(false) {
        ("Not for this computer".to_string(), Tone::Err)
    } else {
        let (l, t) = fit_label(a.get("fit").map(|f| sv(f, "verdict")).unwrap_or(""));
        (l.to_string(), t)
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
    ArtFacts {
        // The web's recommended dot (`mc-rec`, "Recommended for this
        // computer") beside its siblings.
        provider: format!(
            "{}{}",
            if rec { "● " } else { "" },
            provider_label(sv(a, "provider"))
        ),
        quant,
        size,
        weights,
        fit,
    }
}

/// The facts of a row outside the catalog (`mcExtraArtMarkup`).
fn extra_facts(r: &Value) -> ArtFacts {
    let size = num(r, "size_bytes")
        .map(ui_bytes)
        .unwrap_or_else(|| "Size unknown".into());
    let quant = if sv(r, "quant").is_empty() {
        "Not stated".to_string()
    } else {
        sv(r, "quant").to_string()
    };
    let weights = if r.get("loaded").and_then(Value::as_bool) == Some(true) {
        "Downloaded · Loaded"
    } else {
        "Downloaded"
    };
    ArtFacts {
        provider: provider_label(sv(r, "provider")),
        quant,
        size,
        weights: (weights.to_string(), Tone::Ok),
        fit: (String::new(), Tone::Muted),
    }
}

/// The lines under an artifact row (`mcJobMarkup` + the GPU-limit note).
fn under_lines(cv: &Cv, st: &PageState, a: &Value, k: &str) -> Vec<(String, Tone)> {
    let mut out: Vec<(String, Tone)> = Vec::new();
    if a.get("supported_on_host").and_then(Value::as_bool) != Some(false) {
        if let Some(n) = gpu_limit_note(a) {
            out.push((n, Tone::Warn));
        }
    }
    if let Some(j) = cv.jobs.get(k) {
        if job_active(j) {
            out.extend(progress_lines(j));
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
            out.push((format!("The download did not finish. {said}"), Tone::Err));
            let why = {
                let e = sv(j, "error").trim();
                if e.is_empty() {
                    sv(j, "message").trim().to_string()
                } else {
                    e.to_string()
                }
            };
            if !why.is_empty() && why != said {
                out.push((why, Tone::Muted));
            }
        } else if sv(j, "status") == "cancelled" && !cv.installed(a) {
            let r = sv(j, "ended_reason");
            out.push((
                if r.is_empty() {
                    "Download cancelled. Download it again any time.".to_string()
                } else {
                    r.to_string()
                },
                Tone::Muted,
            ));
        }
    }
    if let Some((tone, text)) = st.notices.get(k) {
        out.push((text.clone(), *tone));
    }
    out
}

/// The facts shown under a row when its details are open (`i`): the full
/// id, engine, quantization, size source, presence, fit facts, the
/// download command (the web shows them as the cells' tooltips).
fn detail_facts(a: &Value, alone: bool) -> Vec<String> {
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
    facts
}

/// The model's header line (name · organisation · parameters · licence,
/// the capability tags, the Starter / Hugging Face badges) — the group
/// line drawn above its first artifact row.
pub fn model_header(row: &Value) -> String {
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
    let mut head = if sv(row, "display_name").is_empty() {
        sv(row, "id").to_string()
    } else {
        sv(row, "display_name").to_string()
    };
    if !meta.is_empty() {
        head.push_str(&format!("  {}", meta.join(" · ")));
    }
    let caps: Vec<String> = row_caps(row)
        .iter()
        .map(|c| format!("[{}]", cap_label(c)))
        .collect();
    if !caps.is_empty() {
        head.push_str(&format!("  {}", caps.join(" ")));
    }
    if row.get("starter").and_then(Value::as_bool) == Some(true) {
        head.push_str("  Starter");
    }
    if sv(row, "source") == "hf_search" {
        head.push_str("  Hugging Face");
    }
    head
}

/// The web's column names (the artifact grid).
pub const COLUMNS: [&str; 7] = [
    "Artifact", "Provider", "Quant", "Size", "Weights", "Fit", "Actions",
];

/// The table's columns at width `w` (narrow: Provider/Quant/Size/Fit move
/// to the Artifact cell's second line).
pub fn columns(w: i32) -> Vec<Col> {
    if w < 100 {
        vec![
            Col::new(COLUMNS[0], ColW::Flex { weight: 1, min: 16 }),
            Col::new(COLUMNS[4], ColW::Fit { min: 8, max: 16 }),
            Col::new(COLUMNS[6], ColW::Fit { min: 7, max: 22 }),
        ]
    } else {
        vec![
            Col::new(COLUMNS[0], ColW::Flex { weight: 1, min: 18 }),
            Col::new(COLUMNS[1], ColW::Fit { min: 8, max: 16 }),
            Col::new(COLUMNS[2], ColW::Fit { min: 5, max: 14 }),
            Col::new(COLUMNS[3], ColW::Fit { min: 6, max: 14 }),
            Col::new(COLUMNS[4], ColW::Fit { min: 8, max: 18 }),
            Col::new(COLUMNS[5], ColW::Fit { min: 4, max: 16 }),
            Col::new(COLUMNS[6], ColW::Fit { min: 7, max: 24 }),
        ]
    }
}

/// One DataTable row.
#[allow(clippy::too_many_arguments)]
fn table_row(
    t: &TokenSet,
    k: String,
    f: ArtFacts,
    id: &str,
    strong: bool,
    is_default: bool,
    actions: Vec<Action>,
    note: Vec<(String, Tone)>,
    narrow: bool,
) -> Row {
    use super::w::Ink;
    let id_ink = if strong {
        Ink::new(id, t.text).bold()
    } else {
        Ink::new(id, t.text)
    };
    let mut weights = vec![Ink::new(f.weights.0.clone(), fg_of(t, f.weights.1))];
    if is_default {
        weights.push(Ink::new("\nDefault text model", t.info));
    }
    let cells = if narrow {
        let mut facts = vec![f.provider, f.quant, f.size];
        if !f.fit.0.is_empty() {
            facts.push(f.fit.0.clone());
        }
        vec![
            Cell::Lines(vec![
                vec![id_ink],
                vec![Ink::new(facts.join(" · "), t.text_muted)],
            ]),
            Cell::Text(weights),
            Cell::Actions(actions),
        ]
    } else {
        vec![
            Cell::Text(vec![id_ink]),
            Cell::text(f.provider, t.text),
            Cell::text(f.quant, t.text),
            Cell::text(f.size, t.text),
            Cell::Text(weights),
            Cell::text(f.fit.0.clone(), fg_of(t, f.fit.1)),
            Cell::Actions(actions),
        ]
    };
    let note = if note.is_empty() {
        None
    } else {
        let worst = note
            .iter()
            .map(|(_, t)| *t)
            .max_by_key(|t| match t {
                Tone::Err => 4,
                Tone::Warn => 3,
                Tone::Ok => 2,
                Tone::Info => 1,
                _ => 0,
            })
            .unwrap_or(Tone::Muted);
        let text = note
            .into_iter()
            .map(|(s, _)| s)
            .collect::<Vec<_>>()
            .join("\n");
        Some((text, fg_of(t, worst)))
    };
    Row::new(k, cells).note(note)
}

/// The table rows of the visible list (catalog models grouped under their
/// header line, then "Not in the catalog").
pub fn table_rows(
    t: &TokenSet,
    cv: &Cv,
    st: &PageState,
    admin: bool,
    default: &Option<(String, String)>,
    width: i32,
) -> Vec<Row> {
    let f = &st.filters;
    let narrow = width < 100;
    let mut out = Vec::new();
    for item in visible(cv, f) {
        let alone = arts(item.row).len() == 1;
        let header = model_header(item.row);
        for (i, a) in item.arts.iter().enumerate() {
            let k = art_key(a);
            let mut note = under_lines(cv, st, a, &k);
            if st.expanded.as_deref() == Some(k.as_str()) {
                note.extend(detail_facts(a, alone).into_iter().map(|s| (s, Tone::Muted)));
            }
            let installed = cv.installed(a);
            let mut r = table_row(
                t,
                k,
                art_facts(cv, st, a, alone),
                sv(a, "artifact"),
                a.get("recommended").and_then(Value::as_bool) == Some(true),
                installed && can_be_default(item.row) && is_default(a, default),
                row_actions(cv, st, Some(item.row), a, admin, default),
                note,
                narrow,
            );
            if i == 0 {
                r = r.group(header.clone());
            }
            out.push(r);
        }
    }
    let show_extras = !(f.hf_mode());
    if show_extras {
        for (i, r) in extras(cv, f).into_iter().enumerate() {
            let k = art_key(r);
            let mut note = under_lines(cv, st, r, &k);
            if st.expanded.as_deref() == Some(k.as_str()) {
                note.push((format!("Artifact: {}", sv(r, "artifact")), Tone::Muted));
                if !sv(r, "location").is_empty() {
                    note.push((format!("Size on disk · {}", sv(r, "location")), Tone::Muted));
                }
            }
            let mut row = table_row(
                t,
                k,
                extra_facts(r),
                sv(r, "artifact"),
                false,
                false,
                row_actions(cv, st, None, r, admin, default),
                note,
                narrow,
            );
            if i == 0 {
                row = row.group("Not in the catalog");
            }
            out.push(row);
        }
    }
    out
}

/// The sentences shown INSTEAD of the table (loading, errors, the Hugging
/// Face prompt, empty states), or None when the table shows.
pub fn list_notes(
    cv: &Cv,
    st: &PageState,
    hub: &Loadable<Value>,
    catalog: &Loadable<Value>,
) -> Option<Vec<(String, Tone)>> {
    let f = &st.filters;
    if let Some(q) = &f.hf {
        if q.is_empty() {
            return Some(vec![
                ("Search Hugging Face".into(), Tone::Strong),
                ("Type a model name above and press Enter. Results show as cards you can download, like the catalog.".into(), Tone::Muted),
            ]);
        }
        if st.hub_q.as_deref() == Some(q.as_str()) {
            if let Loadable::Failed(e) = hub {
                return Some(vec![
                    (
                        "Hugging Face could not be searched right now.".into(),
                        Tone::Err,
                    ),
                    (
                        "Check that the gateway host is online, then search again.".into(),
                        Tone::Muted,
                    ),
                    (format!("Details: {}", err_text(e)), Tone::Faint),
                ]);
            }
        }
        if cv.data.is_none() {
            return Some(vec![(
                format!("Searching Hugging Face for “{q}”..."),
                Tone::Muted,
            )]);
        }
        if cv.rows().is_empty() {
            return Some(vec![
                (
                    format!("Hugging Face has no model matching “{q}”."),
                    Tone::Strong,
                ),
                ("Try another name, or fewer words.".into(), Tone::Muted),
            ]);
        }
    }
    if cv.catalog.is_none() {
        if let Loadable::Failed(e) = catalog {
            return Some(vec![
                ("The model catalog did not load.".into(), Tone::Err),
                (err_text(e), Tone::Muted),
            ]);
        }
        return Some(vec![("Loading the model catalog...".into(), Tone::Muted)]);
    }
    None
}

/// The lines between the filters and the list: the count with the active
/// filters (`mcCountMarkup` + `mcActiveMarkup`), the notices and the page
/// message (`mcNoticeMarkup`, the message alert).
pub fn header_lines(cv: &Cv, st: &PageState) -> Vec<(String, Tone)> {
    let f = &st.filters;
    let mut out = Vec::new();
    let have = cv.data.is_some();
    if have {
        let list = visible(cv, f);
        let ex = extras(cv, f);
        let mut t = count_text(cv, &list, ex.len());
        let act = active_words(cv, f);
        if !act.is_empty() {
            t.push_str(&format!("  · {}", act.join(" · ")));
        }
        out.push((t, Tone::Text));
    }
    // Notices: the Hub answered in part; quant_class missing.
    if f.hf_mode() {
        if let Some(hub) = cv.data.and_then(|d| d.get("hub")) {
            if hub.get("ok").and_then(Value::as_bool) == Some(false) {
                out.push((
                    "Hugging Face could not be reached, so these results may be incomplete.".into(),
                    Tone::Warn,
                ));
                out.push((
                    "Check that the gateway host is online, then search again.".into(),
                    Tone::Muted,
                ));
            }
        }
    }
    if have && !cv.quant_reported() {
        out.push((
            "This gateway's catalog does not report quant_class yet.".into(),
            Tone::Warn,
        ));
        out.push(("The 4-bit / 8-bit filter needs it, so it is off. Every model and every artifact is still listed below. Updating AbstractCore on the gateway host turns the filter on.".into(), Tone::Muted));
    }
    if let Some((tone, t)) = &st.message {
        out.push((t.clone(), *tone));
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

/// The footer's verbs for this page (R15: the row actions' keys, the
/// table, the page buttons; the admin ones are gated by [`ADMIN_KEYS`]).
pub fn hints(_non_admin: bool) -> Vec<(&'static str, &'static str)> {
    vec![
        ("↑↓", "rows"),
        ("Enter", "row action"),
        ("Tab", "actions"),
        ("w", "download"),
        ("d", "delete"),
        ("u", "use as default"),
        ("c", "cancel download"),
        ("i", "details"),
        ("/", "search"),
        ("f", "Fits this computer"),
        ("x", "Clear filters"),
        ("Y", "copy id"),
        ("r", "Check again"),
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

type Bump = std::rc::Rc<dyn Fn()>;

/// Where the keyboard was before a rebuild (the table / a filter Select
/// is rebuilt by every change; the control that had the focus takes it
/// back, so the page's keys and the next Tab keep working).
#[derive(Clone, Default)]
struct Focus {
    /// The table (or a row's button) had the keyboard.
    table: std::rc::Rc<std::cell::Cell<bool>>,
    /// The filter Select just picked (its group index).
    select: std::rc::Rc<std::cell::Cell<Option<usize>>>,
    /// The mode segment / the Fits toggle was just used.
    mode: std::rc::Rc<std::cell::Cell<bool>>,
    fits: std::rc::Rc<std::cell::Cell<bool>>,
}

impl Focus {
    fn elsewhere(&self) {
        self.table.set(false);
        self.select.set(None);
        self.mode.set(false);
        self.fits.set(false);
    }
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// The page title and subtitle (the web tab's head).
pub const TITLE: &str = "Models";
pub const SUBTITLE: &str = "Browse, download and delete models that fit this machine";

/// The page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;
    let rev = cx.signal(0u64);
    let bump: Bump = std::rc::Rc::new(move || rev.update(|r| *r += 1));
    // The selected artifact row, by key (sticky across rebuilds; the
    // page state keeps it across a tab switch).
    let sel: Signal<Option<String>> = cx.signal(with_state(|p| p.sel.clone()));
    let top: Signal<usize> = cx.signal(0usize);
    cx.effect(move || {
        let k = sel.get();
        if with_state(|p| p.sel != k) {
            edit(|p| p.sel = k.clone());
        }
    });

    // Providers' "Browse models" hands an engine over through the shared
    // engine filter: the list opens on that engine's builds (the web's
    // engine card opens the catalog filtered the same way).
    {
        let ef = ctx.screens.store.engine_filter;
        let bump = bump.clone();
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
        let bump = bump.clone();
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
        let bump = bump.clone();
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
        let bump = bump.clone();
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

    // Write outcomes (confirmations open on the PAGE scope).
    {
        let ctx_w = ctx.clone();
        let bump = bump.clone();
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
                on_write(&ctx_w, cx, &k, w, bump.clone());
            }
            bump();
        });
    }

    // The search box: a page text field (the caret keeps ←/→); `/` gives
    // it the keyboard, Esc gives the keys back to the list.
    let search = cx.signal(with_state(|p| {
        p.filters.hf.clone().unwrap_or_else(|| p.filters.q.clone())
    }));
    let search_gen = cx.signal(0u64);
    // The mode decides the search box's placeholder: a mode change
    // rebuilds the box (typing never does — the caret stays).
    let mode_rev = cx.signal(0u64);
    let focus = Focus::default();
    let list_id: std::rc::Rc<std::cell::Cell<Option<abstracttui::ui::ViewId>>> =
        std::rc::Rc::new(std::cell::Cell::new(None));
    let list_id_esc = list_id.clone();
    let ctx_s = ctx.clone();
    let bump_s = bump.clone();
    let focus_s = focus.clone();
    let search_box = dyn_view_scoped(LayoutStyle::default().grow(1.0).h(1), move |gcx| {
        let g = search_gen.get();
        let _ = mode_rev.get();
        let focus_s = focus_s.clone();
        let hf = with_state(|p| p.filters.hf_mode());
        let ctx_s = ctx_s.clone();
        let caret_c = ctx_s.ui.caret;
        let (b1, b2) = (bump_s.clone(), bump_s.clone());
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
                    b1();
                }
            })
            .on_submit(move |text: &str| {
                if with_state(|p| p.filters.hf_mode()) {
                    hub_search(&ctx_s, text);
                    b2();
                }
            })
            .element(gcx, &tt);
        let el = super::w::caret_tracked(gcx, caret_c, el).on(Phase::Bubble, move |_e, ev| {
            if matches!(ev, UiEvent::FocusIn) {
                focus_s.elsewhere();
            }
        });
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

    // Page keys (the selected row's accelerators + the page's buttons).
    let mut root = Element::new().style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
        left: 1,
        right: 1,
        top: 0,
        bottom: 0,
    }));
    let chord = |c: char| KeyChord::plain(Key::Char(c));
    {
        let (c, b) = (ctx.clone(), bump.clone());
        root = root.shortcut(chord('r'), move |_| check_again(&c, &b));
    }
    root = root.shortcut(chord('/'), move |_| search_gen.update(|g| *g += 1));
    {
        let b = bump.clone();
        root = root.shortcut(chord('f'), move |_| {
            edit(|p| p.filters.fits = !p.filters.fits);
            b();
        });
    }
    {
        let b = bump.clone();
        root = root.shortcut(chord('x'), move |_| clear_filters(search, &b));
    }
    for (ch, id) in [
        ('w', "download"),
        ('d', "delete"),
        ('u', "default"),
        ('c', "cancel"),
    ] {
        let (c, b) = (ctx.clone(), bump.clone());
        root = root.shortcut(chord(ch), move |_| match with_state(|p| p.sel.clone()) {
            Some(k) => row_action(cx, &c, &k, id, sel, &b),
            None => c.store.notice.set(Some("No model row selected.".into())),
        });
    }
    {
        let b = bump.clone();
        root = root.shortcut(chord('i'), move |_| {
            if let Some(s) = with_state(|p| p.sel.clone()) {
                edit(|p| {
                    p.expanded = if p.expanded.as_deref() == Some(s.as_str()) {
                        None
                    } else {
                        Some(s.clone())
                    };
                });
                b();
            }
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

    root.child(head(cx, ctx, &tt, rev, bump.clone()))
        .child(bar(
            cx,
            ctx,
            &tt,
            rev,
            bump.clone(),
            search,
            search_box,
            mode_rev,
            focus.clone(),
        ))
        .child(filters_region(&tt, store, rev, bump.clone(), focus.clone()))
        .child(body(
            cx,
            ctx,
            &tt,
            rev,
            bump.clone(),
            sel,
            top,
            search,
            list_id,
            focus,
        ))
        .build()
}

/// `r` / [Check again] (`mcAction("refresh")`).
fn check_again(ctx: &Ctx, bump: &Bump) {
    refresh(ctx);
    ctx.store.notice.set(Some("⟳ refreshing Models…".into()));
    bump();
}

/// [Clear filters] / `x`: every filter back to All (the mode stays).
fn clear_filters(search: Signal<String>, bump: &Bump) {
    edit(|p| {
        let hf = p.filters.hf.clone();
        p.filters = Filters {
            hf,
            ..Filters::default()
        };
    });
    search.set(with_state(|p| p.filters.hf.clone().unwrap_or_default()));
    bump();
}

/// Title + subtitle, [Check again] on the right; the host facts under.
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet, rev: Signal<u64>, bump: Bump) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let _ = pcx;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let _ = rev.get();
        let store = ctx.store;
        let snap = snapshot(&store, true);
        let loading = snap.catalog.is_loading() || snap.installed.is_loading();
        let w = page_w(hcx);
        let a = Action::label(
            "refresh",
            if loading {
                "Checking..."
            } else {
                "Check again"
            },
        )
        .key('r');
        let bw = a.width();
        let (c, b) = (ctx.clone(), bump.clone());
        let btn = button(hcx, &tt, &a, On::Page, true, move || check_again(&c, &b));
        let titles = Element::new()
            .style(
                LayoutStyle::column()
                    .width(Dimension::Cells((w - bw - 1).max(10)))
                    .shrink(0.0),
            )
            .child(super::w::paint::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![super::w::Ink::new(TITLE, tt.text).bold()],
                None,
            ))
            .child(super::w::form::sentence(
                &tt,
                SUBTITLE,
                (w - bw - 1).max(10),
                tt.text_muted,
            ))
            .build();
        let host = with_state(|st| with_cv(&snap, st, |cv, _| host_line(cv)));
        let mut col = Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .child(
                Element::new()
                    .style(LayoutStyle::row().shrink(0.0))
                    .child(titles)
                    .child(btn)
                    .build(),
            );
        if let Some(h) = host {
            col = col.child(super::w::form::sentence(&tt, &h, w, tt.text_muted));
        }
        col.build()
    })
}

/// The bar: Catalog | Hugging Face, the search field ([Search] in Hugging
/// Face mode), "Fits this computer".
#[allow(clippy::too_many_arguments)]
fn bar(
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    rev: Signal<u64>,
    bump: Bump,
    search: Signal<String>,
    search_box: View,
    mode_rev: Signal<u64>,
    focus: Focus,
) -> View {
    let tt = *t;
    let _ = pcx;
    let (ctx_m, bump_m, focus_m) = (ctx.clone(), bump.clone(), focus.clone());
    let mode = dyn_view_scoped(LayoutStyle::row().shrink(0.0).h(1), move |mcx| {
        let _ = rev.get();
        let hf = with_state(|p| p.filters.hf_mode());
        let (c, b, f) = (ctx_m.clone(), bump_m.clone(), focus_m.clone());
        let seg = Segmented::new(MODES, Some(if hf { 1 } else { 0 }))
            .autofocus_chosen(focus_m.mode.get())
            .on_pick(move |i| {
                f.elsewhere();
                f.mode.set(true);
                set_mode(&c, i == 1, search);
                mode_rev.update(|r| *r += 1);
                b();
            });
        let w = seg.width();
        Element::new()
            .style(
                LayoutStyle::row()
                    .width(Dimension::Cells(w + 1))
                    .h(1)
                    .shrink(0.0),
            )
            .child(seg.view(mcx, &tt))
            .build()
    });
    let (ctx_r, bump_r, focus_r) = (ctx.clone(), bump.clone(), focus);
    let right = dyn_view_scoped(LayoutStyle::row().shrink(0.0).h(1), move |rcx| {
        let _ = rev.get();
        let (hf, fits) = with_state(|p| (p.filters.hf_mode(), p.filters.fits));
        let mut row = Element::new().style(LayoutStyle::row().h(1).gap(1).shrink(0.0));
        let mut w = 1;
        if hf {
            let a = Action::label("hf_search", "Search");
            w += a.width() + 1;
            let c = ctx_r.clone();
            let b = bump_r.clone();
            row = row.child(button(rcx, &tt, &a, On::Page, true, move || {
                hub_search(&c, &search.get_untracked());
                b();
            }));
        }
        let b = bump_r.clone();
        let f = focus_r.clone();
        let tg = Toggle::new(fits)
            .autofocus(focus_r.fits.get())
            .label("Fits this computer")
            .tip("Fits this computer  (f)")
            .on_change(move |v| {
                f.elsewhere();
                f.fits.set(true);
                edit(|p| p.filters.fits = v);
                b();
            });
        w += tg.width() + 1;
        row = row.child(tg.view(rcx, &tt));
        Element::new()
            .style(
                LayoutStyle::row()
                    .width(Dimension::Cells(w))
                    .h(1)
                    .shrink(0.0),
            )
            .child(
                Element::new()
                    .style(LayoutStyle::default().width(Dimension::Cells(1)))
                    .build(),
            )
            .child(row.build())
            .build()
    });
    Element::new()
        .style(LayoutStyle::row().h(1).shrink(0.0))
        .child(mode)
        .child(search_box)
        .child(right)
        .build()
}

/// The two search modes (the web's mode buttons).
pub const MODES: [&str; 2] = ["Catalog", "Hugging Face"];

/// Switch Catalog / Hugging Face (`mcSetMode`): the catalog query moves to
/// the Hub search and back.
fn set_mode(ctx: &Ctx, hf: bool, search: Signal<String>) {
    let now = with_state(|p| p.filters.hf_mode());
    if now == hf {
        return;
    }
    if !hf {
        edit(|p| p.filters.hf = None);
        search.set(with_state(|p| p.filters.q.clone()));
    } else {
        let q = with_state(|p| p.filters.q.clone());
        edit(|p| p.filters.q.clear());
        hub_search(ctx, &q);
        search.set(q);
    }
}

/// The four filter groups' labels (the web's chip groups).
pub const GROUPS: [&str; 4] = ["Quantization", "Provider", "Capability", "Status"];

/// One filter group's options: (value, label with its count) and the
/// chosen index.
pub fn group_options(cv: &Cv, f: &Filters, group: &str) -> (Vec<(String, String)>, usize) {
    let quant_on = cv.quant_reported();
    let label = |l: &str, n: Option<usize>| match n {
        Some(n) => format!("{l} {n}"),
        None => l.to_string(),
    };
    let (opts, cur): (Vec<(String, String)>, &str) = match group {
        "quant" => (
            QUANT_CHIPS
                .iter()
                .map(|(id, l)| {
                    (
                        id.to_string(),
                        label(
                            l,
                            (quant_on && *id != "all").then(|| chip_count(cv, f, "quant", id)),
                        ),
                    )
                })
                .collect(),
            &f.quant,
        ),
        "provider" => {
            let mut o = vec![("all".to_string(), "All".to_string())];
            for p in providers(cv, f) {
                let n = chip_count(cv, f, "provider", &p);
                o.push((p.clone(), label(&provider_label(&p), Some(n))));
            }
            (o, &f.provider)
        }
        "cap" => {
            let mut o = vec![("all".to_string(), "All".to_string())];
            for c in caps_offered(cv, f) {
                o.push((
                    c.to_string(),
                    label(cap_label(c), Some(chip_count(cv, f, "cap", c))),
                ));
            }
            (o, &f.cap)
        }
        _ => (
            STATUS_CHIPS
                .iter()
                .map(|(id, l)| {
                    (
                        id.to_string(),
                        label(l, (*id != "all").then(|| chip_count(cv, f, "status", id))),
                    )
                })
                .collect(),
            &f.status,
        ),
    };
    let i = opts.iter().position(|(v, _)| v == cur).unwrap_or(0);
    (opts, i)
}

/// One filter group as drawn: (id, options (value, label), chosen, off).
type FilterGroup = (&'static str, Vec<(String, String)>, usize, bool);

/// The filter row: Quantization · Provider · Capability · Status, each a
/// Select (the web's chip rows; a terminal row of chips would wrap).
fn filters_region(t: &TokenSet, store: Store, rev: Signal<u64>, bump: Bump, focus: Focus) -> View {
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |fcx| {
        let _ = rev.get();
        let snap = snapshot(&store, true);
        let w = page_w(fcx);
        let have = with_state(|st| with_cv(&snap, st, |cv, _| cv.data.is_some()));
        if !have {
            return Element::new().style(LayoutStyle::default().h(0)).build();
        }
        let groups: Vec<FilterGroup> = with_state(|st| {
            with_cv(&snap, st, |cv, _| {
                ["quant", "provider", "cap", "status"]
                    .into_iter()
                    .map(|g| {
                        let (o, i) = group_options(cv, &st.filters, g);
                        (g, o, i, g == "quant" && !cv.quant_reported())
                    })
                    .collect()
            })
        });
        let mut rows: Vec<Element> =
            vec![Element::new().style(LayoutStyle::row().h(1).gap(2).shrink(0.0))];
        let mut used = 0;
        for (gi, (g, opts, cur, off)) in groups.into_iter().enumerate() {
            let label = GROUPS[gi];
            let sel_w = opts
                .iter()
                .map(|(_, l)| abstracttui::text::width(l))
                .max()
                .unwrap_or(4)
                + 4;
            let need = abstracttui::text::width(label) + 1 + sel_w + 2;
            if used > 0 && used + need > w {
                rows.push(Element::new().style(LayoutStyle::row().h(1).gap(2).shrink(0.0)));
                used = 0;
            }
            used += need;
            let chosen = fcx.signal(cur);
            let values: Vec<String> = opts.iter().map(|(v, _)| v.clone()).collect();
            let b = bump.clone();
            let group = g.to_string();
            let f = focus.clone();
            let refocus = focus.select.get() == Some(gi);
            let select = abstracttui::app::select::Select::new(
                opts.iter()
                    .map(|(_, l)| abstracttui::app::select::SelectOption::new(l.clone()))
                    .collect(),
            )
            .value(chosen)
            .disabled(off)
            .layout(
                LayoutStyle::default()
                    .width(Dimension::Cells(sel_w))
                    .h(1)
                    .shrink(0.0),
            )
            .on_change(move |i| {
                let Some(v) = values.get(i).cloned() else {
                    return;
                };
                f.elsewhere();
                f.select.set(Some(gi));
                edit(|p| match group.as_str() {
                    "quant" => p.filters.quant = v,
                    "provider" => p.filters.provider = v,
                    "cap" => p.filters.cap = v,
                    _ => p.filters.status = v,
                });
                b();
            })
            .element(fcx, &tt);
            let select = if refocus { select.autofocus() } else { select }.build();
            let tip = if off {
                "This gateway's catalog does not report quant_class yet: the 4-bit / 8-bit filter is off.".to_string()
            } else {
                label.to_string()
            };
            let lab = super::w::paint::fill_line(
                LayoutStyle::default()
                    .width(Dimension::Cells(abstracttui::text::width(label) + 1))
                    .h(1)
                    .shrink(0.0),
                vec![super::w::Ink::new(
                    label,
                    if off { tt.text_faint } else { tt.text_muted },
                )],
                None,
            );
            let item = Element::new()
                .style(LayoutStyle::row().h(1).shrink(0.0))
                .child(lab)
                .child(select);
            let item = super::w::tip::with_tip(fcx, item, if off { tip } else { String::new() });
            let last = rows.pop().expect("row");
            rows.push(last.child(item.build()));
        }
        let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
        for r in rows {
            col = col.child(r.build());
        }
        col.build()
    })
}

/// The count + active filters, the notices, the page message, then the
/// table (or the sentence that replaces it), then "Not in the catalog"'s
/// notices.
#[allow(clippy::too_many_arguments)]
fn body(
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    rev: Signal<u64>,
    bump: Bump,
    sel: Signal<Option<String>>,
    top: Signal<usize>,
    search: Signal<String>,
    list_id: std::rc::Rc<std::cell::Cell<Option<abstracttui::ui::ViewId>>>,
    focus: Focus,
) -> View {
    let ctx = ctx.clone();
    let focus_b = focus.clone();
    let tt = *t;
    let ctx_k = ctx.clone();
    let bump_k = bump.clone();
    let region = dyn_view_scoped(LayoutStyle::column().grow(1.0), move |bcx| {
        let _ = rev.get();
        let store = ctx.store;
        let snap = snapshot(&store, true);
        let admin = !store.conn.with(ConnPhase::is_known_non_admin);
        let vp = crate::ui::page_viewport(bcx).get();
        let w = (vp.w - 2).max(20);
        let mut col = Element::new().style(LayoutStyle::column().grow(1.0));
        let mut used = 0i32;
        let say = |col: Element, text: &str, tone: Tone, used: &mut i32| -> Element {
            let lines = super::w::paint::wrap(text, w);
            *used += lines.len() as i32;
            let v = if tone == Tone::Strong {
                let mut c = Element::new().style(LayoutStyle::column().shrink(0.0));
                for l in lines {
                    c = c.child(super::w::paint::fill_line(
                        LayoutStyle::line(1).shrink(0.0),
                        vec![super::w::Ink::new(l, tt.text).bold()],
                        None,
                    ));
                }
                c.build()
            } else {
                super::w::form::sentence(&tt, text, w, fg_of(&tt, tone))
            };
            col.child(v)
        };
        let (top_lines, notes, rows, empty, extras_notes) = with_state(|st| {
            with_cv(&snap, st, |cv, ierr| {
                let default = current_default(snap.defaults.ready());
                let top_lines = header_lines(cv, st);
                let notes = list_notes(cv, st, &snap.hub, &snap.catalog);
                let rows = if notes.is_none() {
                    table_rows(&tt, cv, st, admin, &default, w)
                } else {
                    Vec::new()
                };
                // No catalog match but rows outside it: only those (the web's
                // `mcExtrasMarkup` alone, no empty sentence).
                let list_empty =
                    notes.is_none() && visible(cv, &st.filters).is_empty() && rows.is_empty();
                let empty = list_empty.then(|| cv.rows().is_empty());
                // "Not in the catalog"'s own notices (no rows of their own).
                let mut x: Vec<(String, Tone)> = Vec::new();
                if notes.is_none() && !st.filters.hf_mode() {
                    if let Some((tone, t)) = st.extra_notice.clone() {
                        x.push((t, tone));
                    }
                    let show_err = st.filters.status != "not_downloaded";
                    if let Some(e) = ierr.filter(|_| show_err) {
                        x.push((
                            "The models outside the catalog could not be listed.".into(),
                            Tone::Warn,
                        ));
                        x.push((e, Tone::Muted));
                    }
                }
                (top_lines, notes, rows, empty, x)
            })
        });
        for (text, tone) in &top_lines {
            col = say(col, text, *tone, &mut used);
        }
        if let Some(notes) = notes {
            for (text, tone) in &notes {
                col = say(col, text, *tone, &mut used);
            }
            return col.build();
        }
        if let Some(catalog_empty) = empty {
            col = say(
                col,
                "No model matches these filters.",
                Tone::Strong,
                &mut used,
            );
            if catalog_empty {
                col = say(
                    col,
                    "This gateway's catalog is empty.",
                    Tone::Muted,
                    &mut used,
                );
            } else {
                col = say(
                    col,
                    "Change or clear the filters to see the rest of the catalog.",
                    Tone::Muted,
                    &mut used,
                );
                let a = Action::label("clear", "Clear filters").key('x');
                let b = bump.clone();
                col = col.child(
                    Element::new()
                        .style(LayoutStyle::row().h(1).shrink(0.0))
                        .child(button(bcx, &tt, &a, On::Page, true, move || {
                            clear_filters(search, &b)
                        }))
                        .build(),
                );
                used += 1;
            }
        }
        let order: Vec<String> = rows.iter().map(|r| r.key.clone()).collect();
        SEL_ORDER.with(|s| *s.borrow_mut() = order.clone());
        // Keep a valid selection (the first row when none / gone).
        let cur = sel.get_untracked();
        if !order.is_empty() && cur.as_ref().is_none_or(|k| !order.contains(k)) {
            sel.set(order.first().cloned());
        }
        if !rows.is_empty() {
            let head_rows = if w < 100 { 8 } else { 6 };
            let extra = extras_notes.len() as i32 + 1;
            let max_rows = (vp.h - head_rows - used - 2 - extra).max(4);
            let (ca, cact) = (ctx.clone(), ctx.clone());
            let (ba, bact) = (bump.clone(), bump.clone());
            let mut table = DataTable::new(columns(w), rows, sel)
                .width(w)
                .max_rows(max_rows)
                .top(top)
                .on_action(move |key, id| row_action(pcx, &ca, key, id, sel, &ba))
                .on_activate(move |key| primary_action(pcx, &cact, key, sel, &bact));
            // The table had the keyboard before this rebuild: it takes it back.
            if focus_b.table.get() {
                table = table.autofocus();
            }
            col = col.child(table.view(bcx, &tt));
        }
        if !extras_notes.is_empty() {
            if order
                .iter()
                .all(|k| with_state(|st| with_cv(&snap, st, |cv, _| !cv.is_extra_key(k))))
            {
                col = say(col, "Not in the catalog", Tone::Muted, &mut used);
            }
            for (text, tone) in &extras_notes {
                col = say(col, text, *tone, &mut used);
            }
        }
        col.build()
    });
    // A focus anchor around the list: the page's keys live from the first
    // frame; ↑/↓ move the selection and Enter runs the row's first action
    // even before Tab enters the table.
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .focusable()
        .autofocus()
        .on(Phase::Capture, move |ectx, ev| {
            if list_id.get().is_none() {
                list_id.set(ectx.current());
            }
            // A key or a press inside the list: the keyboard is here.
            let here = match ev {
                UiEvent::Key(_) => true,
                UiEvent::Mouse(m) => matches!(m.kind, abstracttui::ui::MouseKind::Down(_)),
                _ => false,
            };
            if here {
                focus.elsewhere();
                focus.table.set(true);
            }
        })
        .on(Phase::Bubble, move |ectx, ev| {
            let UiEvent::Key(k) = ev else { return };
            if k.mods.0 != 0 {
                return;
            }
            let order = SEL_ORDER.with(|s| s.borrow().clone());
            if order.is_empty() {
                return;
            }
            let cur = sel
                .get_untracked()
                .and_then(|s| order.iter().position(|x| *x == s))
                .unwrap_or(0);
            let n = order.len();
            let next = match k.key {
                Key::Up => Some(cur.saturating_sub(1)),
                Key::Down => Some((cur + 1).min(n - 1)),
                Key::PageUp => Some(cur.saturating_sub(5)),
                Key::PageDown => Some((cur + 5).min(n - 1)),
                Key::Home => Some(0),
                Key::End => Some(n - 1),
                Key::Enter => {
                    ectx.stop_propagation();
                    primary_action(pcx, &ctx_k, &order[cur], sel, &bump_k);
                    None
                }
                _ => None,
            };
            if let Some(i) = next {
                ectx.stop_propagation();
                sel.set(Some(order[i].clone()));
                if i < top.get_untracked() {
                    top.set(i);
                }
            }
        })
        .child(region)
        .build()
}

/// The host facts line (`mcHostMarkup`).
fn host_line(cv: &Cv) -> Option<String> {
    let p = cv.catalog.and_then(|d| d.get("host_profile"))?;
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
    (!bits.is_empty()).then(|| format!("This computer: {}", bits.join(" · ")))
}

/// The row's actions for key `k` (None when the row is not on the page).
fn actions_of(ctx: &Ctx, k: &str) -> Option<Vec<Action>> {
    let snap = snapshot(&ctx.store, false);
    let admin = admin_of(&ctx.store);
    with_state(|st| {
        with_cv(&snap, st, |cv, _| {
            let default = current_default(snap.defaults.ready());
            let (row, a) = find_art(cv, k)?;
            Some(row_actions(cv, st, row.as_ref(), &a, admin, &default))
        })
    })
}

/// A row action (a click or its key): the row becomes the selection, the
/// action runs; a refused one says why and does nothing.
fn row_action(cx: Scope, ctx: &Ctx, k: &str, id: &str, sel: Signal<Option<String>>, bump: &Bump) {
    edit(|p| p.sel = Some(k.to_string()));
    if sel.get_untracked().as_deref() != Some(k) {
        sel.set(Some(k.to_string()));
    }
    let Some(actions) = actions_of(ctx, k) else {
        return;
    };
    let Some(a) = actions.into_iter().find(|a| a.id == id) else {
        return;
    };
    if let Err(why) = a.enabled {
        ctx.store.notice.set(Some(why));
        return;
    }
    let what = match id {
        "download" => Act::Download,
        "delete" => Act::Delete,
        "default" => Act::Default,
        "cancel" => Act::Cancel,
        _ => return,
    };
    act(cx, ctx, what, bump.clone());
    bump();
}

/// Enter / double-click: the row's first action (a refused one says why).
fn primary_action(cx: Scope, ctx: &Ctx, k: &str, sel: Signal<Option<String>>, bump: &Bump) {
    let Some(actions) = actions_of(ctx, k) else {
        return;
    };
    let first = actions
        .iter()
        .find(|a| a.is_enabled())
        .or(actions.first())
        .cloned();
    if let Some(a) = first {
        row_action(cx, ctx, k, a.id, sel, bump);
    }
}

thread_local! {
    static SEL_ORDER: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
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

fn act(cx: Scope, ctx: &Ctx, what: Act, bump: Bump) {
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
            // The web's two steps (`dlCancelMarkup`): a click only asks.
            Confirm::danger(CANCEL_QUESTION, "Stop download", "Keep downloading").open(
                cx,
                ctx.ui,
                move || {
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
                    bump();
                },
            );
        }
    }
}

/// The web's cancel question (`dlCancelMarkup`).
pub const CANCEL_QUESTION: &str = "Stop this download?";

fn on_write(ctx: &Ctx, cx: Scope, k: &str, w: WriteState, bump: Bump) {
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
                let key_n = art.to_string();
                let found = with_state(|st| with_cv(&snap, st, |cv, _| find_art(cv, &key_s)));
                let Some((_, a)) = found else { return };
                let (provider, artifact) = (
                    sv(&a, "provider").to_string(),
                    sv(&a, "artifact").to_string(),
                );
                let (b_yes, b_no) = (bump.clone(), bump.clone());
                Confirm::danger(sentence, "Delete", "Keep").open_with(
                    cx,
                    ctx.ui,
                    move || {
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
                        b_yes();
                    },
                    move || {
                        // Keep: back to the trash button, nothing sent.
                        edit(|p| {
                            p.del.remove(&key_n);
                        });
                        b_no();
                    },
                );
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
                let said = if extra {
                    format!("Deleted {artifact}.{freed}")
                } else {
                    format!("Download deleted.{freed}")
                };
                edit(|p| {
                    p.del.remove(art);
                    p.deleted.insert(art.to_string());
                    if extra && p.sel.as_deref() == Some(art) {
                        p.sel = neighbour.clone();
                    }
                    if extra {
                        p.extra_notice = Some((Tone::Ok, said.clone()));
                    } else {
                        p.notices.insert(art.to_string(), (Tone::Ok, said.clone()));
                    }
                });
                super::w::toast(ctx, cx, said);
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
                edit(|p| p.message = Some((Tone::Ok, text.clone())));
                super::w::toast(ctx, cx, text);
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
