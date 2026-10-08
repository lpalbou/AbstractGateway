//! OpenAI API page (sidebar MODELS, after Providers; key `8`) — the web
//! console's "OpenAI API" page (console_ui.py `oai*`) in the terminal.
//!
//! Contract `gateway_openai_api_v1` (routes/core_endpoint.py), the SAME
//! routes and bodies as the web page:
//!
//! | what | route |
//! |------|-------|
//! | the page | `GET /openai-api` (role-shaped: admin also gets access, reach, run-as, warnings) |
//! | Recent requests | `GET /openai-api/logs?limit=25` (every 5 s while the page is shown) |
//! | one request | `GET /openai-api/logs/{request_id}` (redacted request + response) |
//! | Endpoint / Authentication / Who can connect / run as | `POST /admin/core-endpoint {enabled \| access \| reach \| open_account}` |
//! | Restart | `POST /admin/core-endpoint/restart` |
//! | Check setup | `POST /admin/core-endpoint/check` |
//! | New key | `POST /me/token/rotate` |
//!
//! The API key is the signed-in person's gateway token. No route answers a
//! stored token: the page shows the token THIS console signed in with
//! (the web page shows the copy its browser kept at sign-in), checked
//! against the gateway's fingerprint (SHA-256, first 12 hex). Masked by
//! default; `v` reveals it, `y` copies it in clear, the examples embed it
//! masked on screen and in clear when copied. New key replaces the token
//! (shown once) and the console reconnects with it.
//!
//! Every sentence is the web page's. Layout: the overview (Status,
//! Connect your app, Access for an admin, Docs) scrolls in the upper
//! pane; Recent requests is a wrapping table below (Enter opens a row:
//! the recorded request and response). Tab moves between the two.

use std::time::Duration;

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use serde_json::Value;

use super::util::wrap_text;
use super::w::action::On;
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, Ink, Row as WRow, Segmented, Toggle};
use super::widths::ColRule;
use super::Ctx;
use crate::store::json::WriteState;
use crate::store::{ConnPhase, Loadable};
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

/// The page's read (`GET /openai-api`).
pub const KEY_PAGE: &str = "openai";
/// The request log (`GET /openai-api/logs?limit=25`).
pub const KEY_LOGS: &str = "openai.logs";
/// One request's record: `openai.log.<request_id>`.
pub const KEY_LOG_PREFIX: &str = "openai.log.";
/// The admin access writes (`POST /admin/core-endpoint`).
pub const KEY_CHANGE: &str = "openai.change";
pub const KEY_RESTART: &str = "openai.restart";
pub const KEY_CHECK: &str = "openai.check";
pub const KEY_NEW_KEY: &str = "openai.newkey";

pub const PATH_PAGE: &str = "/openai-api";
pub const PATH_LOGS: &str = "/openai-api/logs?limit=25";
pub const PATH_CHANGE: &str = "/admin/core-endpoint";
pub const PATH_RESTART: &str = "/admin/core-endpoint/restart";
pub const PATH_CHECK: &str = "/admin/core-endpoint/check";
pub const PATH_NEW_KEY: &str = "/me/token/rotate";

/// The web page's log cadence (`OAI_LOG_REFRESH_MS`).
pub const LOG_REFRESH: Duration = Duration::from_secs(5);
/// The masked key (`OAI_MASK`).
pub const MASK: &str = "••••••••••••••••";

/// The Authentication options (`OAI_AUTH`).
pub const AUTH: [(&str, &str, &str); 2] = [
    (
        "token",
        "Protected (API key)",
        "Apps send a gateway token as their API key.",
    ),
    (
        "open",
        "Open (no key)",
        "Apps connect without a key. Cloud providers still need one.",
    ),
];

/// The Who-can-connect texts (`OAI_REACH_TEXT`).
pub fn reach_text(id: &str) -> &'static str {
    match id {
        "machine" => "Apps on this computer.",
        "network" => "Also phones and computers on your Wi-Fi or office network.",
        "tailnet" => "Also your devices on Tailscale.",
        "anywhere" => "Also the internet, through your proxy or tunnel.",
        _ => "",
    }
}

/// The example tabs (`OAI_SNIPPETS`).
pub const SNIPPETS: [(&str, &str); 3] =
    [("curl", "curl"), ("python", "Python"), ("js", "JavaScript")];

// ---------------------------------------------------------------------
// The key: SHA-256 fingerprint check (no crate: the gateway's 12 hex)
// ---------------------------------------------------------------------

/// SHA-256 of `data` (FIPS 180-4), hex. Small and dependency-free: the
/// page only needs the gateway's fingerprint (`sha256(token)[:12]`).
pub fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut msg = data.to_vec();
    let bits = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bits.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let mut v = h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v[7] = v[6];
            v[6] = v[5];
            v[5] = v[4];
            v[4] = v[3].wrapping_add(t1);
            v[3] = v[2];
            v[2] = v[1];
            v[1] = v[0];
            v[0] = t1.wrapping_add(t2);
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

/// Whether this console's token is the account's current one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyCheck {
    /// The fingerprint matches (or the gateway gave none to compare).
    Match,
    /// The gateway's fingerprint differs: the token changed elsewhere.
    Stale,
    /// The console holds no token.
    Missing,
}

/// The token the page may show as the key: the console's own, unless it
/// is missing or no longer the account's (the web `oaiKeptToken`).
pub fn kept_token(d: &Value, token: Option<&str>) -> (Option<String>, KeyCheck) {
    let Some(t) = token.map(str::trim).filter(|t| !t.is_empty()) else {
        return (None, KeyCheck::Missing);
    };
    let fp = d
        .pointer("/key/fingerprint")
        .and_then(Value::as_str)
        .unwrap_or("");
    if fp.is_empty() || sha256_hex(t.as_bytes()).starts_with(fp) {
        (Some(t.to_string()), KeyCheck::Match)
    } else {
        (None, KeyCheck::Stale)
    }
}

// ---------------------------------------------------------------------
// Pure folds (unit-tested): the overview lines, the example, the log rows
// ---------------------------------------------------------------------

/// A line's ink.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Title,
    Text,
    Muted,
    Faint,
    Ok,
    Warn,
    Err,
    Accent,
    Code,
}

/// One overview line before wrapping.
#[derive(Clone, Debug, PartialEq)]
pub struct Ln {
    pub text: String,
    pub tone: Tone,
    /// Continuation indent when the line wraps.
    pub indent: usize,
}

#[cfg(test)]
fn lni(text: impl Into<String>, tone: Tone, indent: usize) -> Ln {
    Ln {
        text: text.into(),
        tone,
        indent,
    }
}

/// The page's own state beside the gateway's document.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct ViewState {
    /// The token this console holds (effective credentials).
    pub token: Option<String>,
    pub reveal: bool,
    /// Index into [`SNIPPETS`].
    pub snippet: usize,
    /// A write in flight: "enabled", "access:open", "reach:network",
    /// "open_account", "restart", "check", "new-key".
    pub busy: Option<String>,
    /// The access card's result line (tone, text).
    pub notice: Option<(Tone, String)>,
    /// The key row's result line.
    pub key_notice: Option<(Tone, String)>,
    /// The last Check setup's rows.
    pub checks: Option<Vec<Value>>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn b(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}

fn arr<'a>(v: &'a Value, k: &str) -> Vec<&'a Value> {
    v.get(k)
        .and_then(Value::as_array)
        .map(|a| a.iter().collect())
        .unwrap_or_default()
}

pub fn is_admin(d: &Value) -> bool {
    s(d, "role") == "admin"
}

/// The example request (`oaiSnippet`): `shown` masks the key unless
/// revealed; `clear` = what Copy example puts on the clipboard.
pub fn snippet(kind: &str, d: &Value, kept: Option<&str>, reveal: bool, clear: bool) -> String {
    let base = s(d, "base_url");
    let open = s(d, "access") == "open" && kept.is_none();
    let key = if open {
        "not-needed".to_string()
    } else if let Some(k) = kept {
        if clear || reveal {
            k.to_string()
        } else {
            MASK.to_string()
        }
    } else {
        "YOUR_GATEWAY_TOKEN".to_string()
    };
    let model = match s(d, "example_model") {
        "" => "provider/model",
        m => m,
    };
    match kind {
        "python" => format!(
            "from openai import OpenAI\n\nclient = OpenAI(base_url=\"{base}\", api_key=\"{key}\")\nreply = client.chat.completions.create(\n    model=\"{model}\",\n    messages=[{{\"role\": \"user\", \"content\": \"Hello\"}}],\n)\nprint(reply.choices[0].message.content)"
        ),
        "js" => format!(
            "import OpenAI from \"openai\";\n\nconst client = new OpenAI({{ baseURL: \"{base}\", apiKey: \"{key}\" }});\nconst reply = await client.chat.completions.create({{\n  model: \"{model}\",\n  messages: [{{ role: \"user\", content: \"Hello\" }}],\n}});\nconsole.log(reply.choices[0].message.content);"
        ),
        _ => format!(
            "curl {base}/chat/completions \\\n  -H \"Authorization: Bearer {key}\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{{\"model\": \"{model}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hello\"}}]}}'"
        ),
    }
}

/// `HH:MM:SS` of an ISO timestamp in local time (the web's
/// `toLocaleTimeString`); the input unchanged when it does not parse.
pub fn local_time(ts: &str) -> String {
    match crate::localtime::parse_iso_epoch(ts) {
        Some(e) => {
            let t = (e + crate::localtime::local_offset(e)).rem_euclid(86_400);
            format!("{:02}:{:02}:{:02}", t / 3600, (t % 3600) / 60, t % 60)
        }
        None => ts.to_string(),
    }
}

/// The Recent requests columns (`oaiLogsCard`).
pub fn log_rules() -> Vec<ColRule> {
    vec![
        ColRule::head("Time", 8),
        ColRule::head("Client", 6),
        ColRule::tail("Model", 8),
        ColRule::head("Tokens", 6),
        ColRule::head("Latency", 6),
        ColRule::head("Status", 3),
        ColRule::head("Run", 3),
    ]
}

/// One log row's cells (`oaiLogsCard`).
pub fn log_cells(r: &Value) -> Vec<String> {
    let num = |k: &str| r.get(k).filter(|v| !v.is_null()).map(|v| v.to_string());
    let tokens: Vec<String> = [
        num("prompt_tokens").map(|n| format!("{n} in")),
        num("completion_tokens").map(|n| format!("{n} out")),
    ]
    .into_iter()
    .flatten()
    .collect();
    let tokens = if tokens.is_empty() {
        "—".to_string()
    } else {
        tokens.join(" · ")
    };
    let client = [s(r, "client"), s(r, "ip")]
        .into_iter()
        .filter(|x| !x.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    vec![
        local_time(s(r, "ts")),
        client,
        match s(r, "model") {
            "" => "—".to_string(),
            m => m.to_string(),
        },
        tokens,
        num("duration_ms")
            .map(|n| format!("{n} ms"))
            .unwrap_or_else(|| "—".into()),
        num("status")
            .map(|n| n.trim_matches('"').to_string())
            .unwrap_or_else(|| "—".into()),
        if s(r, "observer_path").is_empty() {
            "—".to_string()
        } else {
            "Open".to_string()
        },
    ]
}

/// `oaiJson`: one recorded side as text.
pub fn side_text(side: Option<&Value>) -> String {
    let Some(side) = side.filter(|v| v.is_object()) else {
        return "Not recorded.".to_string();
    };
    if let Some(body) = side.get("body") {
        return serde_json::to_string_pretty(body).unwrap_or_default();
    }
    if let Some(t) = side.get("text").and_then(Value::as_str) {
        return t.to_string();
    }
    if let Some(o) = side.get("omitted") {
        let o = o
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| o.to_string());
        let bytes = side.get("bytes").map(|v| v.to_string()).unwrap_or_default();
        return format!("Not kept: {o} ({bytes} bytes).");
    }
    if side.get("bytes").and_then(Value::as_i64) == Some(0) {
        "Empty.".to_string()
    } else {
        "Not recorded.".to_string()
    }
}

fn side_head(label: &str, side: Option<&Value>, which: &str) -> String {
    let mut out = label.to_string();
    if let Some(n) = side.and_then(|v| v.get("bytes")).and_then(Value::as_i64) {
        out.push_str(&format!(" · {n} bytes"));
    }
    if side
        .and_then(|v| v.get("truncated"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        out.push_str(" · first 256 KB shown");
    }
    if which == "response" {
        if let Some(n) = side
            .and_then(|v| v.pointer("/body/assembled_from_stream"))
            .filter(|v| !v.is_null() && v.as_bool() != Some(false))
        {
            out.push_str(&format!(" · assembled from {n} stream events"));
        }
    }
    out
}

/// A code line's leading spaces as no-break spaces: the table's word wrap
/// would otherwise fold the JSON indentation away.
pub fn keep_indent(l: &str) -> String {
    let n = l.len() - l.trim_start_matches(' ').len();
    format!("{}{}", "\u{a0}".repeat(n), &l[n..])
}

/// The Observer address of a log row (the web link is relative to the
/// gateway it was served from).
pub fn observer_url(d: Option<&Value>, path: &str) -> String {
    if path.starts_with("http://") || path.starts_with("https://") {
        return path.to_string();
    }
    let base = d.map(|d| s(d, "base_url")).unwrap_or("");
    let origin = base.strip_suffix("/v1").unwrap_or(base);
    format!("{origin}{path}")
}

/// An opened row's lines (`oaiDetailMarkup`): `det` = the
/// `GET /openai-api/logs/{id}` read for it.
pub fn detail_lines(det: &Loadable<Value>, d: Option<&Value>) -> Vec<String> {
    match det {
        Loadable::NotAsked | Loadable::Loading => vec!["Reading the request...".into()],
        Loadable::Failed(e) => vec!["Could not read this request.".into(), e.message.clone()],
        Loadable::Ready(v) => {
            let row = v.get("row").cloned().unwrap_or(Value::Null);
            let mut facts: Vec<String> = Vec::new();
            let mp = format!("{} {}", s(&row, "method"), s(&row, "path"))
                .trim()
                .to_string();
            if !mp.is_empty() {
                facts.push(mp);
            }
            if !s(&row, "client").is_empty() {
                facts.push(format!("as {}", s(&row, "client")));
            }
            for k in ["ip", "user_agent"] {
                if !s(&row, k).is_empty() {
                    facts.push(s(&row, k).to_string());
                }
            }
            let mut out = vec![format!(
                "{}. Keys and tokens were removed when this was recorded.",
                facts.join(" · ")
            )];
            // Observer first: an opened row shows it without scrolling.
            match row.get("observer_path").and_then(Value::as_str) {
                Some(p) if !p.is_empty() => {
                    out.push(format!("Open in Observer  {}  (o copies)", observer_url(d, p)))
                }
                _ => out.push(
                    "Open in Observer — Not part of a run: Observer shows the requests that a run made."
                        .into(),
                ),
            }
            for (label, which) in [("Request", "request"), ("Response", "response")] {
                let side = row.get(which);
                out.push(side_head(label, side, which));
                for l in side_text(side).lines() {
                    out.push(format!("  {}", keep_indent(l)));
                }
            }
            out
        }
    }
}

/// The note under "Recent requests" (`oaiLogsCard`).
pub fn logs_note(scope: &str) -> String {
    format!(
        "{} Open a row to see the request and the response. Refreshes every 5 s; kept as long as the audit log.",
        if scope == "own" {
            "Your requests, newest first."
        } else {
            "Every account's requests, newest first."
        }
    )
}

/// The saved line after an access change (`OAI_SAVED`).
pub fn saved_text(field: &str, d: &Value) -> String {
    let label_of = |list: &str, id: &str| {
        arr(d, list)
            .into_iter()
            .find(|o| s(o, "id") == id)
            .map(|o| s(o, "label").to_string())
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| id.to_string())
    };
    match field {
        "enabled" => {
            if b(d, "enabled") {
                "Running: apps can connect now.".into()
            } else {
                "Stopped: open requests ended.".into()
            }
        }
        "access" => format!(
            "Saved: {}. Applies now.",
            if s(d, "access") == "open" {
                "Open (no key)"
            } else {
                "Protected (API key)"
            }
        ),
        "reach" => format!(
            "Saved: {}. Applies now.",
            label_of("reach_options", s(d, "reach"))
        ),
        _ => format!(
            "Saved: requests without a key run as {}.",
            label_of("open_account_options", s(d, "open_account"))
        ),
    }
}

/// The New key confirmation (the web `oaiStore.confirmKey` sentence).
pub const NEW_KEY_SENTENCE: &str = "Make a new key? It replaces your gateway token: apps, devices and other browsers using the old one stop working. The gateway shows the new key once; this console keeps it for this session.";

// ---------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------

/// The page's key hints (footer). The admin verbs are absent for a user
/// (the web page hides their controls).
pub fn hints(non_admin: bool) -> Vec<(&'static str, &'static str)> {
    let mut v = vec![
        ("Tab", "next control"),
        ("Enter", "press · open request"),
        ("b", "Copy base URL"),
        ("v", "Show/Hide key"),
        ("y", "Copy key"),
        ("n", "New key"),
    ];
    if !non_admin {
        v.extend([
            ("e", "Endpoint"),
            ("x", "Restart"),
            ("h", "Check setup"),
            ("a", "Authentication"),
            ("w", "Who can connect"),
            ("u", "run as"),
        ]);
    }
    v.extend([
        ("s", "example"),
        ("c", "Copy example"),
        ("f", "full record"),
        ("o", "Open in Observer"),
        ("r", "refresh"),
    ]);
    v
}

fn send_get(ctx: &Ctx, key: &str, path: &str) {
    ctx.send(Cmd::Json(JsonCmd::get(key, path)));
}

/// `r` on this page (and the first look): the page and its log.
pub fn refresh(ctx: &Ctx) {
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        return;
    }
    ctx.store.json.set(KEY_PAGE, Loadable::Loading);
    send_get(ctx, KEY_PAGE, PATH_PAGE);
    send_get(ctx, KEY_LOGS, PATH_LOGS);
}

/// The admin write `POST /admin/core-endpoint {field: value}`.
fn change(ctx: &Ctx, busy: Signal<Option<String>>, field: &str, value: Value) {
    if busy.get_untracked().is_some() {
        return;
    }
    let tag = match (field, &value) {
        ("access", Value::String(v)) | ("reach", Value::String(v)) => format!("{field}:{v}"),
        _ => field.to_string(),
    };
    busy.set(Some(tag));
    let mut body = serde_json::Map::new();
    body.insert(field.to_string(), value.clone());
    ctx.store
        .json
        .set_write(KEY_CHANGE, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: KEY_CHANGE.into(),
        method: "POST".into(),
        path: PATH_CHANGE.into(),
        body: Value::Object(body),
        slow: false,
        label: format!(
            "OpenAI API: {field} = {}",
            value
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(|| value.to_string())
        ),
        reload: vec![(KEY_PAGE.into(), PATH_PAGE.into())],
        journal: true,
    }));
}

fn post(ctx: &Ctx, key: &str, path: &str, label: &str, slow: bool) {
    ctx.store.json.set_write(key, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: key.into(),
        method: "POST".into(),
        path: path.into(),
        body: serde_json::json!({}),
        slow,
        label: label.into(),
        reload: if key == KEY_CHECK {
            vec![]
        } else {
            vec![(KEY_PAGE.into(), PATH_PAGE.into())]
        },
        journal: key != KEY_CHECK,
    }));
}

fn tone_ink(t: &TokenSet, tone: Tone) -> abstracttui::base::Rgba {
    match tone {
        Tone::Title => t.text,
        Tone::Text => t.text,
        Tone::Muted => t.text_muted,
        Tone::Faint => t.text_faint,
        Tone::Ok => t.ok,
        Tone::Warn => t.warn,
        Tone::Err => t.error,
        Tone::Accent => t.accent,
        Tone::Code => t.info,
    }
}

/// Wrap the overview lines to `w` cells (continuations indented).
pub fn wrap_lines(lines: &[Ln], w: usize) -> Vec<(String, Tone)> {
    let mut out = Vec::new();
    for l in lines {
        if l.text.is_empty() {
            out.push((String::new(), l.tone));
            continue;
        }
        // Leading spaces are layout (an indented note): kept; the
        // continuation lines take the line's indent. Nothing is dropped.
        let lead = (l.text.len() - l.text.trim_start().len()).min(w / 2);
        let ind = l.indent.min(w / 2).max(lead);
        let pieces = wrap_text(l.text.trim_start(), w.saturating_sub(ind).max(10));
        for (k, c) in pieces.into_iter().enumerate() {
            let pad = if k == 0 { lead } else { ind };
            out.push((format!("{}{c}", " ".repeat(pad)), l.tone));
        }
    }
    out
}

// ---------------------------------------------------------------------
// R15 (DESIGN-TUI.md §3.8): the web's cards top-down with real controls
// — Status (Copy, the Endpoint toggle, Restart, Check setup), Connect
// your app (Copy, Show/Hide, Copy, New key), Access (Authentication
// segments, Requests without a key run as / Who can connect pickers),
// Docs (the two links, curl | Python | JavaScript, Copy example) — then
// Recent requests as a table (Time opens the recorded request and
// response; Run opens Observer). One action list (`page_actions`,
// `log_actions`) is the single source for the buttons, keys and tests.
// ---------------------------------------------------------------------

/// The web's words of the controls (console_ui.py `oai*`).
pub const ENDPOINT_TIP: &str = "Answer OpenAI API requests at the base URL";
pub const RESTART_TIP: &str = "End open requests and keep serving";
pub const CHECK_TIP: &str = "Check settings, Core and models";
pub const SHOW_KEY_TIP: &str = "Show your API key";
pub const HIDE_KEY_TIP: &str = "Hide your API key";
pub const NOT_IN_A_RUN: &str = "This request was not part of a run";

/// The page's buttons for document `d` (`kept`: the console holds the
/// account's key; `reveal`: shown in clear), in screen order.
pub fn page_actions(d: &Value, kept: bool, reveal: bool) -> Vec<Action> {
    let admin = is_admin(d);
    let mut out = vec![Action::label("copy_base", "Copy")
        .key('b')
        .tooltip("Copy base URL")];
    if admin {
        out.push(
            Action::label("restart", "Restart")
                .key('x')
                .tooltip(RESTART_TIP)
                .refused((!b(d, "enabled")).then(|| "Restart needs the endpoint on.".to_string())),
        );
        out.push(
            Action::label("check", "Check setup")
                .key('h')
                .tooltip(CHECK_TIP),
        );
    }
    let own = d
        .pointer("/key/own_token")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if own {
        if kept {
            out.push(if reveal {
                Action::label("reveal", "Hide")
                    .key('v')
                    .tooltip(HIDE_KEY_TIP)
            } else {
                Action::label("reveal", "Show")
                    .key('v')
                    .tooltip(SHOW_KEY_TIP)
            });
            out.push(
                Action::label("copy_key", "Copy")
                    .key('y')
                    .tooltip("Copy API key"),
            );
        }
        out.push(
            Action::label("new_key", "New key")
                .key('n')
                .tooltip(NEW_KEY_SENTENCE),
        );
    }
    if admin
        && arr(d, "warnings")
            .into_iter()
            .any(|w| s(w, "id") == "listener")
    {
        out.push(Action::label("network", "Network").key('N').tooltip(
            "Who can reach this gateway (this computer, local network, internet) and its addresses",
        ));
    }
    out.push(
        Action::link("doc_openai", "OpenAI API compatibility")
            .tooltip("endpoints and parameters this gateway supports"),
    );
    out.push(Action::link("doc_core", "AbstractCore server").tooltip("the engine behind it"));
    out.push(Action::label("copy_example", "Copy example").key('c'));
    out
}

/// A request row's actions: open the recorded request and response (the
/// web's Time toggle), and Open in Observer (runs only).
pub fn log_actions(r: &Value) -> Vec<Action> {
    vec![
        Action::link("details", local_time(s(r, "ts"))).tooltip(format!(
            "Show the request and response of {}",
            local_time(s(r, "ts"))
        )),
        Action::label("observer", "Open")
            .key('o')
            .tooltip("Open in Observer")
            .refused(
                s(r, "observer_path")
                    .is_empty()
                    .then(|| NOT_IN_A_RUN.to_string()),
            ),
    ]
}

/// Open `url` in a browser here, or (no display / no opener) copy it and
/// say so.
fn open_or_copy(ctx: &Ctx, url: &str) {
    if ctx.no_display.is_none() && ctx.screens.open_url(url).is_ok() {
        return;
    }
    copy_to_clipboard(url.to_string());
    ctx.store
        .notice
        .set(Some(format!("copied {url} — open it in your browser")));
}

/// The page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;
    let st = Page {
        reveal: cx.signal(false),
        snippet: cx.signal(0usize),
        busy: cx.signal(None),
        notice: cx.signal(None),
        key_notice: cx.signal(None),
        checks: cx.signal(None),
        last_field: cx.signal(String::new()),
        log_key: cx.signal(None),
        reach_pick: abstracttui::app::select::SelectHandle::new(),
        account_pick: abstracttui::app::select::SelectHandle::new(),
    };

    // First look once connected; a reconnect (new key) reads again.
    {
        let ctx_l = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected && store.json.slots.with(|m| !m.contains_key(KEY_PAGE)) {
                refresh(&ctx_l);
            }
        });
    }
    // The log refreshes while the page is shown (the interval dies with
    // the page's scope).
    {
        let ctx_p = ctx.clone();
        let _ = abstracttui::reactive::interval(cx, LOG_REFRESH, move || {
            if ctx_p.store.conn.with_untracked(ConnPhase::is_connected) {
                send_get(&ctx_p, KEY_LOGS, PATH_LOGS);
            }
        });
    }
    install_write_effects(cx, ctx, &st);

    let keys_ctx = ctx.clone();
    let st_keys = st.clone();
    let st_body = st.clone();
    let body_ctx = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 && !matches!(k.key, Key::Char('N')) {
                    return;
                }
                if handle_key(cx, &keys_ctx, &st_keys, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            let t = tt;
            let w = (crate::ui::page_viewport(cx).get().w - 2).max(20);
            super::workflows::page_head(&t, TITLE, SUBTITLE, w, Vec::new())
        }))
        .child(dyn_view_scoped(
            LayoutStyle::column()
                .gap(0)
                .grow(3.0)
                .basis(Dimension::Cells(0)),
            move |gcx| {
                let t = use_theme(gcx).get().tokens;
                cards(gcx, cx, &body_ctx, &t, &st_body)
            },
        ))
        .child(logs_region(cx, ctx, &tt, &st))
        .build()
}

/// The web page's title and subtitle.
pub const TITLE: &str = "OpenAI API";
pub const SUBTITLE: &str = "Let apps use your models through one OpenAI-compatible address";

/// The page's own state beside the gateway's document.
#[derive(Clone)]
struct Page {
    reveal: Signal<bool>,
    snippet: Signal<usize>,
    busy: Signal<Option<String>>,
    notice: Signal<Option<(Tone, String)>>,
    key_notice: Signal<Option<(Tone, String)>>,
    checks: Signal<Option<Vec<Value>>>,
    last_field: Signal<String>,
    /// The request row the keyboard is on (its request id).
    log_key: Signal<Option<String>>,
    reach_pick: abstracttui::app::select::SelectHandle,
    account_pick: abstracttui::app::select::SelectHandle,
}

/// Write outcomes → the web page's sentences (success toasts; refusals
/// inline in their card).
fn install_write_effects(cx: Scope, ctx: &Ctx, st: &Page) {
    let store = ctx.store;
    let st = st.clone();
    let ctx_w = ctx.clone();
    cx.effect(move || {
        if let Some(w) = store.json.write(KEY_CHANGE) {
            if !w.is_pending() {
                let field = st.last_field.get_untracked();
                match w {
                    WriteState::Done(v) => {
                        let text = saved_text(&field, &v);
                        super::w::toast(&ctx_w, cx, text.clone());
                        st.notice.set(Some((Tone::Ok, text)));
                    }
                    WriteState::Failed(e) => st
                        .notice
                        .set(Some((Tone::Err, format!("Not saved: {}", e.message)))),
                    WriteState::Pending => {}
                }
                st.busy.set(None);
                store.json.set_write(KEY_CHANGE, None);
            }
        }
        if let Some(w) = store.json.write(KEY_RESTART) {
            if !w.is_pending() {
                match w {
                    WriteState::Done(v) => {
                        let n = v.get("ended_requests").and_then(Value::as_i64).unwrap_or(0);
                        let what = if n > 0 {
                            format!("{n} open request{} ended", if n == 1 { "" } else { "s" })
                        } else {
                            "no request was open".into()
                        };
                        let text = format!("Restarted: {what}.");
                        super::w::toast(&ctx_w, cx, text.clone());
                        st.notice.set(Some((Tone::Ok, text)));
                    }
                    WriteState::Failed(e) => st
                        .notice
                        .set(Some((Tone::Err, format!("Not restarted: {}", e.message)))),
                    WriteState::Pending => {}
                }
                st.busy.set(None);
                store.json.set_write(KEY_RESTART, None);
            }
        }
        if let Some(w) = store.json.write(KEY_CHECK) {
            if !w.is_pending() {
                match w {
                    WriteState::Done(v) => st.checks.set(Some(
                        v.get("checks")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default(),
                    )),
                    WriteState::Failed(e) => st.checks.set(Some(vec![serde_json::json!({
                        "id": "error", "ok": false, "text": e.message
                    })])),
                    WriteState::Pending => {}
                }
                st.busy.set(None);
                store.json.set_write(KEY_CHECK, None);
            }
        }
        if let Some(w) = store.json.write(KEY_NEW_KEY) {
            if !w.is_pending() {
                match w {
                    WriteState::Done(v) => {
                        let token = s(&v, "token").to_string();
                        if token.is_empty() {
                            st.key_notice.set(Some((
                                Tone::Err,
                                "No new key: The gateway answered without the new key.".into(),
                            )));
                        } else {
                            // The console now signs in with the new key
                            // (the old one stopped working).
                            ctx_w.ui.conn_token.set(token);
                            st.reveal.set(true);
                            st.key_notice.set(Some((
                                Tone::Ok,
                                "New key made: your old one stopped working. Copy it now: the gateway shows a key only once.".into(),
                            )));
                            ctx_w.connect_now();
                        }
                    }
                    WriteState::Failed(e) => st
                        .key_notice
                        .set(Some((Tone::Err, format!("No new key: {}", e.message)))),
                    WriteState::Pending => {}
                }
                st.busy.set(None);
                store.json.set_write(KEY_NEW_KEY, None);
            }
        }
    });
}

fn doc(ctx: &Ctx) -> Option<Value> {
    ctx.store.json.get_untracked(KEY_PAGE).ready().cloned()
}

/// Admin-only verbs: the reason for anyone else.
fn admin_only(ctx: &Ctx, what: &str) -> bool {
    match doc(ctx) {
        Some(d) if is_admin(&d) => true,
        Some(_) => {
            ctx.store
                .notice
                .set(Some(format!("Only an admin can {what}.")));
            false
        }
        None => {
            ctx.store
                .notice
                .set(Some("Reading the OpenAI API settings...".into()));
            false
        }
    }
}

/// A page action (a click or its key).
fn page_action(cx: Scope, ctx: &Ctx, st: &Page, id: &str) {
    let store = ctx.store;
    let say = |m: &str| store.notice.set(Some(m.to_string()));
    let Some(d) = doc(ctx) else {
        say("Reading the OpenAI API settings...");
        return;
    };
    let kept = kept_token(&d, ctx.effective_credentials().1.as_deref()).0;
    if let Some(a) = page_actions(&d, kept.is_some(), st.reveal.get_untracked())
        .into_iter()
        .find(|a| a.id == id)
    {
        if let Err(why) = a.enabled {
            say(&why);
            return;
        }
    }
    match id {
        "copy_base" => {
            let u = s(&d, "base_url").to_string();
            copy_to_clipboard(u.clone());
            say(&format!("copied {u}"));
        }
        "restart" => {
            if !admin_only(ctx, "start or stop it") || st.busy.get_untracked().is_some() {
                return;
            }
            if !b(&d, "enabled") {
                say("Restart needs the endpoint on.");
                return;
            }
            st.busy.set(Some("restart".into()));
            st.notice.set(None);
            post(ctx, KEY_RESTART, PATH_RESTART, "OpenAI API: restart", false);
        }
        "check" => {
            if !admin_only(ctx, "check the setup") || st.busy.get_untracked().is_some() {
                return;
            }
            st.busy.set(Some("check".into()));
            post(ctx, KEY_CHECK, PATH_CHECK, "OpenAI API: check setup", true);
        }
        "reveal" => st.reveal.update(|r| *r = !*r),
        "copy_key" => match kept {
            Some(k) => {
                copy_to_clipboard(k);
                say("API key copied to the clipboard");
            }
            None => say("No key to copy: New key makes one."),
        },
        "new_key" => {
            if st.busy.get_untracked().is_some() {
                return;
            }
            let c2 = ctx.clone();
            let (busy, key_notice) = (st.busy, st.key_notice);
            super::w::Confirm::danger(NEW_KEY_SENTENCE, "New key", "Cancel").open(
                cx,
                ctx.ui,
                move || {
                    busy.set(Some("new-key".into()));
                    key_notice.set(None);
                    c2.store
                        .json
                        .set_write(KEY_NEW_KEY, Some(WriteState::Pending));
                    c2.send(Cmd::Json(JsonCmd::Send {
                        key: KEY_NEW_KEY.into(),
                        method: "POST".into(),
                        path: PATH_NEW_KEY.into(),
                        body: serde_json::json!({}),
                        slow: false,
                        label: "OpenAI API: new key".into(),
                        reload: vec![],
                        journal: false,
                    }));
                },
            );
        }
        "network" => super::shell::go(ctx, super::SCREEN_NETWORK),
        "doc_openai" | "doc_core" => {
            let ptr = if id == "doc_openai" {
                "/docs/openai_api"
            } else {
                "/docs/abstractcore"
            };
            match d.pointer(ptr).and_then(Value::as_str) {
                Some(u) if !u.is_empty() => open_or_copy(ctx, u),
                _ => say("This gateway did not give that link."),
            }
        }
        "copy_example" => {
            let kind = SNIPPETS[st.snippet.get_untracked() % SNIPPETS.len()].0;
            copy_to_clipboard(snippet(kind, &d, kept.as_deref(), false, true));
            say("example copied to the clipboard");
        }
        _ => {}
    }
}

/// The Endpoint toggle (admins).
fn switch_endpoint(ctx: &Ctx, st: &Page) {
    if admin_only(ctx, "start or stop it") {
        let on = doc(ctx).map(|d| b(&d, "enabled")).unwrap_or(false);
        st.last_field.set("enabled".into());
        st.notice.set(None);
        change(ctx, st.busy, "enabled", Value::Bool(!on));
    }
}

/// Authentication: Protected (API key) | Open (no key).
fn set_access(ctx: &Ctx, st: &Page, id: &str) {
    if !admin_only(ctx, "change the authentication") {
        return;
    }
    if doc(ctx).map(|d| s(&d, "access") == id).unwrap_or(false) {
        return;
    }
    st.last_field.set("access".into());
    st.notice.set(None);
    change(ctx, st.busy, "access", Value::String(id.into()));
}

/// Who can connect / run as: a picked option (a locked one says why).
fn pick_option(ctx: &Ctx, st: &Page, field: &str, list: &str, id: &str) {
    let Some(d) = doc(ctx) else { return };
    let opt = arr(&d, list)
        .into_iter()
        .find(|o| s(o, "id") == id)
        .cloned();
    let Some(o) = opt else { return };
    if b(&o, "selected") {
        return;
    }
    if o.get("available").and_then(Value::as_bool) == Some(false) {
        st.notice
            .set(Some((Tone::Warn, s(&o, "reason").to_string())));
        return;
    }
    st.last_field.set(field.into());
    st.notice.set(None);
    change(ctx, st.busy, field, Value::String(id.into()));
}

fn handle_key(cx: Scope, ctx: &Ctx, st: &Page, key: Key) -> bool {
    match key {
        Key::Char('e') => switch_endpoint(ctx, st),
        Key::Char('x') => page_action(cx, ctx, st, "restart"),
        Key::Char('h') => page_action(cx, ctx, st, "check"),
        Key::Char('a') => {
            let next = if doc(ctx).map(|d| s(&d, "access") == "open").unwrap_or(false) {
                "token"
            } else {
                "open"
            };
            set_access(ctx, st, next);
        }
        Key::Char('w') => {
            if admin_only(ctx, "change who can connect") && !st.reach_pick.open() {
                ctx.store
                    .notice
                    .set(Some("Who can connect: scroll to the Access card.".into()));
            }
        }
        Key::Char('u') => {
            if admin_only(ctx, "choose who requests without a key run as") {
                if doc(ctx).map(|d| s(&d, "access") != "open").unwrap_or(true) {
                    ctx.store.notice.set(Some(
                        "Requests without a key exist in Open mode only.".into(),
                    ));
                } else {
                    st.account_pick.open();
                }
            }
        }
        Key::Char('v') => page_action(cx, ctx, st, "reveal"),
        Key::Char('y') => page_action(cx, ctx, st, "copy_key"),
        Key::Char('b') => page_action(cx, ctx, st, "copy_base"),
        Key::Char('n') => page_action(cx, ctx, st, "new_key"),
        Key::Char('s') => st.snippet.update(|i| *i = (*i + 1) % SNIPPETS.len()),
        Key::Char('c') => page_action(cx, ctx, st, "copy_example"),
        Key::Char('o') => log_action(cx, ctx, st, None, "observer"),
        Key::Char('f') => log_action(cx, ctx, st, None, "details"),
        _ => return false,
    }
    true
}

/// A titled card: the title (bold), its note, then its rows.
fn card(
    t: &TokenSet,
    title: &str,
    note: &str,
    right: Option<(View, i32)>,
    w: i32,
    rows: Vec<View>,
) -> View {
    let tw = abstracttui::text::width(title);
    let mut head = Element::new()
        .style(LayoutStyle::row().h(1).shrink(0.0))
        .child(super::w::fill_line(
            LayoutStyle::default().w(tw).h(1).shrink(0.0),
            vec![Ink::new(title, t.text).bold()],
            None,
        ));
    if let Some((r, rw)) = right {
        head = head
            .child(
                Element::new()
                    .style(
                        LayoutStyle::default()
                            .w((w - tw - rw).max(1))
                            .h(1)
                            .shrink(1.0),
                    )
                    .build(),
            )
            .child(r);
    }
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(head.build());
    if !note.is_empty() {
        col = col.child(sentence(t, note, w, t.text_muted));
    }
    for r in rows {
        col = col.child(r);
    }
    col.child(super::w::fill_line(
        LayoutStyle::line(1).shrink(0.0),
        vec![],
        None,
    ))
    .build()
}

/// `label  value  [buttons…]` (an address row) in `w` cells; the buttons
/// are `(view, width)`.
fn addr_row(t: &TokenSet, w: i32, label: &str, value: Vec<Ink>, buttons: Vec<(View, i32)>) -> View {
    let bw: i32 = buttons.iter().map(|(_, x)| x + 1).sum();
    let vw = (w - 11 - bw).max(8);
    let mut row = Element::new()
        .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
        .child(super::w::fill_line(
            LayoutStyle::default().w(10).h(1).shrink(0.0),
            vec![Ink::new(label, t.text_muted)],
            None,
        ))
        .child(super::w::fill_line(
            LayoutStyle::default().w(vw).h(1).shrink(0.0),
            value,
            None,
        ));
    for (b, _) in buttons {
        row = row.child(b);
    }
    row.build()
}

fn cards(cx: Scope, pcx: Scope, ctx: &Ctx, t: &TokenSet, st: &Page) -> View {
    let store = ctx.store;
    let w = (crate::ui::page_viewport(cx).get().w - 3).max(20);
    let page_doc = store.json.get(KEY_PAGE);
    let anchor = |v: View| -> View {
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .focusable()
            .autofocus()
            .child(v)
            .build()
    };
    if !store.conn.with(ConnPhase::is_connected) {
        return anchor(sentence(
            t,
            "not connected — probe the gateway on 1 Connection first",
            w,
            t.text_faint,
        ));
    }
    let d = match &page_doc {
        Loadable::Ready(d) => d.clone(),
        Loadable::Failed(e) => {
            return anchor(
                Element::new()
                    .style(LayoutStyle::column().shrink(0.0))
                    .child(sentence(
                        t,
                        "Could not read the OpenAI API settings.",
                        w,
                        t.error,
                    ))
                    .child(sentence(t, &e.message, w, t.error))
                    .child(sentence(t, "r Try again", w, t.text_muted))
                    .build(),
            )
        }
        _ => {
            return anchor(sentence(
                t,
                "Reading the OpenAI API settings...",
                w,
                t.text_muted,
            ))
        }
    };
    let admin = is_admin(&d);
    let reveal = st.reveal.get();
    let busy = st.busy.get();
    let token = ctx.effective_credentials().1;
    let (kept, check) = kept_token(&d, token.as_deref());
    let acts = page_actions(&d, kept.is_some(), reveal);
    let btnw = |id: &str| -> (View, i32) {
        let a = acts
            .iter()
            .find(|a| a.id == id)
            .cloned()
            .unwrap_or_else(|| Action::label("none", ""));
        let c = ctx.clone();
        let st2 = st.clone();
        let aid = a.id;
        let wd = a.width();
        (
            super::w::action::button(cx, t, &a, On::Page, true, move || {
                page_action(pcx, &c, &st2, aid)
            }),
            wd,
        )
    };
    let btn = |id: &str| -> View { btnw(id).0 };
    let base = s(&d, "base_url").to_string();
    let running = b(&d, "running");
    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));

    // ---- Status
    let pill = super::w::fill_line(
        LayoutStyle::default().w(10).h(1).shrink(0.0),
        vec![if running {
            Ink::new("● Running", t.ok)
        } else {
            Ink::new("○ Stopped", t.text_muted)
        }],
        None,
    );
    let mut rows = vec![addr_row(
        t,
        w,
        "Base URL",
        vec![Ink::new(base.clone(), t.text)],
        vec![btnw("copy_base")],
    )];
    if !admin {
        rows.push(sentence(
            t,
            &format!(
                "{} Only an admin can start or stop it.",
                if running {
                    "Apps can connect now."
                } else {
                    "Stopped: apps can't connect."
                }
            ),
            w,
            t.text_muted,
        ));
    } else {
        let c = ctx.clone();
        let st2 = st.clone();
        let label = if busy.as_deref() == Some("enabled") {
            "Saving..."
        } else {
            "Endpoint"
        };
        let mut restart = acts
            .iter()
            .find(|a| a.id == "restart")
            .cloned()
            .expect("restart");
        if busy.as_deref() == Some("restart") {
            restart.label = "Restarting...".into();
        }
        let mut check_a = acts
            .iter()
            .find(|a| a.id == "check")
            .cloned()
            .expect("check");
        if busy.as_deref() == Some("check") {
            check_a.label = "Checking...".into();
        }
        let (c1, s1, c2, s2) = (ctx.clone(), st.clone(), ctx.clone(), st.clone());
        rows.push(
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Toggle::new(b(&d, "enabled"))
                        .label(label)
                        .tip(format!("{ENDPOINT_TIP}  (e)"))
                        .on_change(move |_| switch_endpoint(&c, &st2))
                        .view(cx, t),
                )
                .child(super::w::action::button(
                    cx,
                    t,
                    &restart,
                    On::Page,
                    true,
                    move || page_action(pcx, &c1, &s1, "restart"),
                ))
                .child(super::w::action::button(
                    cx,
                    t,
                    &check_a,
                    On::Page,
                    true,
                    move || page_action(pcx, &c2, &s2, "check"),
                ))
                .build(),
        );
        rows.push(sentence(
            t,
            "Endpoint: answers apps at this address. Restart ends open requests.",
            w,
            t.text_muted,
        ));
        if let Some(checks) = st.checks.get() {
            for c in checks {
                let (pill, ink) = match c.get("ok").and_then(Value::as_bool) {
                    Some(true) => ("OK", t.ok),
                    Some(false) => ("Fix", t.error),
                    None => ("Note", t.warn),
                };
                rows.push(sentence(t, &format!("{pill:<5}{}", s(&c, "text")), w, ink));
            }
        }
        if let Some((tone, text)) = st.notice.get() {
            if matches!(text.as_str(), x if x.starts_with("Restarted") || x.starts_with("Not restarted"))
            {
                rows.push(sentence(t, &text, w, tone_ink(t, tone)));
            }
        }
    }
    col = col.child(card(
        t,
        "Status",
        "One address for every OpenAI-compatible app.",
        Some((pill, 10)),
        w,
        rows,
    ));

    // ---- Connect your app
    let key = d.get("key").cloned().unwrap_or(Value::Null);
    let mut rows = Vec::new();
    if key.get("allowed").and_then(Value::as_bool) == Some(false) {
        rows.push(sentence(
            t,
            "The OpenAI API is off for your account. An admin can turn it on in Accounts.",
            w,
            t.warn,
        ));
    }
    rows.push(addr_row(
        t,
        w,
        "Base URL",
        vec![Ink::new(base.clone(), t.text)],
        vec![btnw("copy_base")],
    ));
    if !b(&key, "own_token") {
        rows.push(addr_row(
            t,
            w,
            "API key",
            vec![Ink::new("The gateway admin token", t.text)],
            vec![],
        ));
        rows.push(sentence(
            t,
            "The token this gateway was started with.",
            w,
            t.text_muted,
        ));
    } else {
        match &kept {
            Some(k) => {
                let shown = if reveal { k.clone() } else { MASK.to_string() };
                rows.push(addr_row(
                    t,
                    w,
                    "API key",
                    vec![Ink::new(shown, t.info)],
                    vec![btnw("reveal"), btnw("copy_key"), btnw("new_key")],
                ));
                rows.push(sentence(
                    t,
                    "Your gateway token: apps use it as their API key and act as you.",
                    w,
                    t.text_muted,
                ));
            }
            None => {
                let why = if check == KeyCheck::Stale {
                    "Your token changed since you signed in here, so this console's copy no longer works."
                } else {
                    "This console doesn't have your token: it connected without one."
                };
                rows.push(addr_row(
                    t,
                    w,
                    "API key",
                    vec![Ink::new(MASK, t.info)],
                    vec![btnw("new_key")],
                ));
                rows.push(sentence(
                    t,
                    &format!("{why} New key makes one; the gateway shows it once."),
                    w,
                    t.warn,
                ));
            }
        }
    }
    if let Some((tone, text)) = st.key_notice.get() {
        rows.push(sentence(t, &text, w, tone_ink(t, tone)));
    }
    if s(&d, "access") == "open" {
        rows.push(sentence(
            t,
            "Open mode: SDKs still ask for a key; any text works, for example not-needed.",
            w,
            t.text_muted,
        ));
    }
    rows.push(sentence(
        t,
        "Model names are provider/model, as listed at /v1/models.",
        w,
        t.text_muted,
    ));
    col = col.child(card(
        t,
        "Connect your app",
        "Paste these two values into any OpenAI SDK or app.",
        None,
        w,
        rows,
    ));

    // ---- Access (admins)
    if admin {
        col = col.child(access_card(cx, ctx, t, st, &d, w));
    }

    // ---- Docs
    let mut rows = vec![
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(btn("doc_openai"))
            .child(super::w::fill_line(
                LayoutStyle::default().grow(1.0).h(1),
                vec![Ink::new(
                    "endpoints and parameters this gateway supports",
                    t.text_muted,
                )],
                None,
            ))
            .build(),
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(btn("doc_core"))
            .child(super::w::fill_line(
                LayoutStyle::default().grow(1.0).h(1),
                vec![Ink::new("the engine behind it", t.text_muted)],
                None,
            ))
            .build(),
    ];
    if let Some(sp) = d.get("support") {
        for (label, k, ink) in [
            ("Supported:", "tested", t.ok),
            ("Also served, with an engine set up:", "served", t.text),
            ("Not yet:", "not_yet", t.text_faint),
        ] {
            let items: Vec<&str> = arr(sp, k).into_iter().filter_map(Value::as_str).collect();
            if !items.is_empty() {
                rows.push(sentence(
                    t,
                    &format!("{label} {}", items.join(" · ")),
                    w,
                    ink,
                ));
            }
        }
    }
    rows.push(
        Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(
                Segmented::new(SNIPPETS.iter().map(|(_, l)| *l), None)
                    .bind(st.snippet)
                    .view(cx, t),
            )
            .child(btn("copy_example"))
            .build(),
    );
    let kind = SNIPPETS[st.snippet.get() % SNIPPETS.len()].0;
    for l in snippet(kind, &d, kept.as_deref(), reveal, false).lines() {
        rows.push(sentence(t, &format!("  {l}"), w, t.info));
    }
    let note = if kept.is_some() {
        "The key is hidden here; Copy example includes it."
    } else if s(&d, "access") == "open" {
        ""
    } else {
        "Replace YOUR_GATEWAY_TOKEN with your token."
    };
    if !note.is_empty() {
        rows.push(sentence(t, note, w, t.text_muted));
    }
    col = col.child(card(
        t,
        "Docs",
        &format!(
            "What is supported, and a first request with your base URL{}.",
            if kept.is_some() { " and your key" } else { "" }
        ),
        None,
        w,
        rows,
    ));
    Scroll::new(col.build())
        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
        .scrollbar_auto_hide(true)
        .view(cx)
}

fn access_card(cx: Scope, ctx: &Ctx, t: &TokenSet, st: &Page, d: &Value, w: i32) -> View {
    let busy = st.busy.get();
    let access = s(d, "access").to_string();
    let mut rows = Vec::new();
    // Authentication: two segments (one Tab stop each, A1).
    let chosen = AUTH.iter().position(|(id, _, _)| *id == access);
    let mut seg = Segmented::new(
        AUTH.iter().map(|(id, label, _)| {
            if busy.as_deref() == Some(&format!("access:{id}")) {
                "Saving...".to_string()
            } else {
                label.to_string()
            }
        }),
        chosen,
    );
    for (i, (_, _, text)) in AUTH.iter().enumerate() {
        seg = seg.tip(i, *text);
    }
    let (c, st2) = (ctx.clone(), st.clone());
    seg = seg.on_pick(move |i| set_access(&c, &st2, AUTH[i].0));
    rows.push(super::w::field_row(
        t,
        "Authentication",
        18,
        seg.view(cx, t),
    ));
    if let Some((_, _, text)) = AUTH.iter().find(|(id, _, _)| *id == access) {
        rows.push(indent(t, text, w, t.text_muted));
    }
    // Requests without a key run as (Open mode).
    if access == "open" {
        let opts = arr(d, "open_account_options");
        let ids: Vec<String> = opts.iter().map(|o| s(o, "id").to_string()).collect();
        let cur = opts.iter().position(|o| b(o, "selected")).unwrap_or(0);
        let chosen = cx.signal(cur);
        let (c, st2) = (ctx.clone(), st.clone());
        let ids2 = ids.clone();
        let select = abstracttui::app::select::Select::new(
            opts.iter()
                .map(|o| {
                    let locked = o.get("available").and_then(Value::as_bool) == Some(false);
                    let label = if locked {
                        format!(
                            "{} — {}",
                            s(o, "label"),
                            match s(o, "reason") {
                                "" => "unavailable",
                                r => r,
                            }
                        )
                    } else {
                        s(o, "label").to_string()
                    };
                    abstracttui::app::select::SelectOption::new(label)
                        .disabled(locked && !b(o, "selected"))
                })
                .collect(),
        )
        .value(chosen)
        .handle(&st.account_pick)
        .disabled(busy.is_some())
        .layout(
            LayoutStyle::default()
                .w((w - 34).clamp(20, 60))
                .h(1)
                .shrink(0.0),
        )
        .on_change(move |i| {
            if let Some(id) = ids2.get(i) {
                pick_option(&c, &st2, "open_account", "open_account_options", id);
            }
        })
        .element(cx, t)
        .build();
        rows.push(super::w::field_row(
            t,
            "Requests without a key run as",
            31,
            select,
        ));
        rows.push(indent(
            t,
            "Guest may use the models (chat, embeddings, speech, images) with no tools, files or attachments. An account brings its own models and providers, and its requests show in its log. Never an admin.",
            w,
            t.text_muted,
        ));
    }
    // Who can connect.
    let reach: Vec<&Value> = arr(d, "reach_options")
        .into_iter()
        .filter(|o| o.get("shown").and_then(Value::as_bool) != Some(false))
        .collect();
    let ids: Vec<String> = reach.iter().map(|o| s(o, "id").to_string()).collect();
    let cur = reach.iter().position(|o| b(o, "selected")).unwrap_or(0);
    let chosen = cx.signal(cur);
    let (c, st2) = (ctx.clone(), st.clone());
    let ids2 = ids.clone();
    let select = abstracttui::app::select::Select::new(
        reach
            .iter()
            .map(|o| {
                let id = s(o, "id");
                let locked = o.get("available").and_then(Value::as_bool) == Some(false);
                let text = if locked && !s(o, "reason").is_empty() {
                    s(o, "reason").to_string()
                } else {
                    reach_text(id).to_string()
                };
                abstracttui::app::select::SelectOption::new(s(o, "label").to_string())
                    .hint(text)
                    .disabled(locked && !b(o, "selected"))
            })
            .collect(),
    )
    .value(chosen)
    .handle(&st.reach_pick)
    .disabled(busy.is_some())
    .layout(
        LayoutStyle::default()
            .w((w - 20).clamp(20, 60))
            .h(1)
            .shrink(0.0),
    )
    .on_change(move |i| {
        if let Some(id) = ids2.get(i) {
            pick_option(&c, &st2, "reach", "reach_options", id);
        }
    })
    .element(cx, t)
    .build();
    rows.push(super::w::field_row(t, "Who can connect", 18, select));
    if let Some(o) = reach.get(cur) {
        rows.push(indent(t, reach_text(s(o, "id")), w, t.text_muted));
    }
    for wn in arr(d, "warnings") {
        let ink = if s(wn, "tone") == "warn" {
            t.warn
        } else {
            t.text_muted
        };
        rows.push(sentence(t, s(wn, "text"), w, ink));
        if s(wn, "id") == "listener" {
            let a = Action::label("network", "Network").tooltip("Who can reach this gateway (this computer, local network, internet) and its addresses");
            let c = ctx.clone();
            rows.push(super::w::action::button(
                cx,
                t,
                &a,
                On::Page,
                true,
                move || super::shell::go(&c, super::SCREEN_NETWORK),
            ));
        }
    }
    if let Some((tone, text)) = st.notice.get() {
        if !(text.starts_with("Restarted") || text.starts_with("Not restarted")) {
            rows.push(sentence(t, &text, w, tone_ink(t, tone)));
        }
    }
    card(
        t,
        "Access",
        "Changes apply immediately. Who may use the API with their own key: the OpenAI API switch of each account, in Accounts.",
        None,
        w,
        rows,
    )
}

/// A sentence indented under a field row's control.
fn indent(t: &TokenSet, text: &str, w: i32, ink: Rgba) -> View {
    Element::new()
        .style(LayoutStyle::row().shrink(0.0))
        .child(
            Element::new()
                .style(LayoutStyle::default().w(18).h(1).shrink(0.0))
                .build(),
        )
        .child(sentence(t, text, (w - 18).max(10), ink))
        .build()
}

fn log_rows(ctx: &Ctx) -> Vec<Value> {
    ctx.store
        .json
        .get_untracked(KEY_LOGS)
        .ready()
        .and_then(|v| v.get("rows").and_then(Value::as_array).cloned())
        .unwrap_or_default()
}

/// A request action: the full record (a modal) or Open in Observer. `rid`
/// None = the row the keyboard is on.
fn log_action(cx: Scope, ctx: &Ctx, st: &Page, rid: Option<&str>, id: &str) {
    let store = ctx.store;
    let rows = log_rows(ctx);
    let want = rid
        .map(str::to_string)
        .or_else(|| st.log_key.get_untracked());
    let row = match &want {
        Some(k) => rows.iter().find(|r| s(r, "request_id") == k).cloned(),
        None => rows.first().cloned(),
    };
    let Some(row) = row else {
        store.notice.set(Some("No requests yet.".into()));
        return;
    };
    st.log_key.set(Some(s(&row, "request_id").to_string()));
    match id {
        "observer" => match row.get("observer_path").and_then(Value::as_str) {
            Some(p) if !p.is_empty() => {
                let url = observer_url(doc(ctx).as_ref(), p);
                open_or_copy(ctx, &url);
            }
            _ => store.notice.set(Some(
                "Not part of a run: Observer shows the requests that a run made.".into(),
            )),
        },
        _ => open_record(cx, ctx, row),
    }
}

/// The recorded request and response of `row` in a form modal that
/// scrolls; Copy request / Copy response / Open in Observer / Close.
fn open_record(cx: Scope, ctx: &Ctx, row: Value) {
    let store = ctx.store;
    let rid = s(&row, "request_id").to_string();
    let key = format!("{KEY_LOG_PREFIX}{rid}");
    if !matches!(store.json.get_untracked(&key), Loadable::Ready(_)) {
        store.json.set(&key, Loadable::Loading);
        send_get(ctx, &key, &format!("/openai-api/logs/{}", urlencode(&rid)));
    }
    let title = format!(
        "Request {} · {}",
        local_time(s(&row, "ts")),
        log_cells(&row)[1]
    );
    let c = ctx.clone();
    super::w::FormModal::new(title).size(110, 34).open(
        ctx,
        cx,
        move |mcx, close, _guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let key2 = key.clone();
            let copy = move |which: &'static str| {
                let det = store.json.get_untracked(&key2);
                if let Some(v) = det.ready() {
                    copy_to_clipboard(side_text(v.pointer(&format!("/row/{which}"))));
                    store
                        .notice
                        .set(Some(format!("{which} copied to the clipboard")));
                }
            };
            let copy2 = copy.clone();
            let key3 = key.clone();
            let body = dyn_view(LayoutStyle::column().shrink(0.0), move || {
                let t = abstracttui::app::current_theme().tokens;
                let d = store.json.get(KEY_PAGE).ready().cloned();
                let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                for l in detail_lines(&store.json.get(&key3), d.as_ref()) {
                    col = col.child(sentence(&t, &l, inner_w - 1, t.text));
                }
                col.build()
            });
            let observer = log_actions(&row)
                .into_iter()
                .find(|a| a.id == "observer")
                .map(|mut a| {
                    a.label = "Open in Observer".into();
                    a
                })
                .expect("observer");
            let (c1, row1) = (c.clone(), row.clone());
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    Scroll::new(body)
                        .layout(LayoutStyle::default().grow(1.0).min_h(4))
                        .element(mcx, &t)
                        .build(),
                )
                .child(super::w::form::button_row(vec![
                    super::w::action::button(
                        mcx,
                        &t,
                        &Action::label("copy_request", "Copy request"),
                        On::Raised,
                        true,
                        move || copy("request"),
                    ),
                    super::w::action::button(
                        mcx,
                        &t,
                        &Action::label("copy_response", "Copy response"),
                        On::Raised,
                        true,
                        move || copy2("response"),
                    ),
                    super::w::action::button(mcx, &t, &observer, On::Raised, true, move || {
                        match row1.get("observer_path").and_then(Value::as_str) {
                            Some(p) if !p.is_empty() => {
                                let url = observer_url(doc(&c1).as_ref(), p);
                                open_or_copy(&c1, &url);
                            }
                            _ => {}
                        }
                    }),
                    super::w::action::button(
                        mcx,
                        &t,
                        &Action::label("close", "Close"),
                        On::Raised,
                        true,
                        move || close(),
                    ),
                ]))
                .build()
        },
    );
}

/// "Recent requests": the note and the table (Time opens the record, Run
/// opens Observer).
fn logs_region(pcx: Scope, ctx: &Ctx, t: &TokenSet, st: &Page) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let st = st.clone();
    dyn_view_scoped(
        LayoutStyle::column()
            .gap(0)
            .grow(2.0)
            .basis(Dimension::Cells(0)),
        move |gcx| {
            let t = tt;
            let store = ctx.store;
            let vp = crate::ui::page_viewport(gcx).get();
            let w = (vp.w - 2).max(20);
            let logs = store.json.get(KEY_LOGS);
            let scope = logs
                .ready()
                .map(|v| s(v, "scope").to_string())
                .unwrap_or_default();
            let mut col = Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .child(super::w::section(&t, "Recent requests"))
                .child(sentence(&t, &logs_note(&scope), w, t.text_muted));
            if let Loadable::Failed(e) = &logs {
                col = col.child(sentence(
                    &t,
                    &format!("Could not read the request log. {}", e.message),
                    w,
                    t.error,
                ));
            }
            let rows_v: Option<Vec<Value>> = logs.ready().map(|v| {
                v.get("rows")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default()
            });
            let narrow = w < 100;
            let mut cols = vec![Col::new("Time", ColW::Fit { min: 8, max: 10 })];
            if !narrow {
                cols.push(Col::new("Client", ColW::Fit { min: 6, max: 24 }));
            }
            cols.push(Col::new("Model", ColW::Flex { weight: 1, min: 8 }));
            if !narrow {
                cols.push(Col::new("Tokens", ColW::Fit { min: 6, max: 18 }));
                cols.push(Col::new("Latency", ColW::Fit { min: 7, max: 10 }));
            }
            cols.push(Col::new("Status", ColW::Fit { min: 6, max: 6 }));
            cols.push(Col::new("Run", ColW::Fit { min: 4, max: 6 }));
            let rows: Vec<WRow> = rows_v
                .clone()
                .unwrap_or_default()
                .iter()
                .map(|r| {
                    let c = log_cells(r);
                    let status = r.get("status").and_then(Value::as_i64).unwrap_or(0);
                    let ok = (200..300).contains(&status);
                    let mut cells = vec![Cell::Link {
                        label: c[0].clone(),
                        action: "details",
                        tip: Some(format!(
                            "Show the request and response of {}  (Enter)",
                            c[0]
                        )),
                    }];
                    if !narrow {
                        cells.push(Cell::text(c[1].clone(), t.text));
                    }
                    cells.push(Cell::text(c[2].clone(), t.text));
                    if !narrow {
                        cells.push(Cell::text(c[3].clone(), t.text));
                        cells.push(Cell::text(c[4].clone(), t.text));
                    }
                    cells.push(Cell::text(c[5].clone(), if ok { t.ok } else { t.error }));
                    cells.push(if s(r, "observer_path").is_empty() {
                        Cell::text("—", t.text_faint)
                    } else {
                        Cell::Link {
                            label: "Open".into(),
                            action: "observer",
                            tip: Some("Open in Observer  (o)".into()),
                        }
                    });
                    WRow::new(s(r, "request_id").to_string(), cells).dim(!ok)
                })
                .collect();
            let empty = if rows_v.is_some() {
                "No requests yet."
            } else {
                "Reading the log..."
            };
            if st.log_key.get_untracked().is_none() {
                if let Some(first) = rows_v.as_ref().and_then(|r| r.first()) {
                    st.log_key.set(Some(s(first, "request_id").to_string()));
                }
            }
            let (ca, sa, ce, se) = (ctx.clone(), st.clone(), ctx.clone(), st.clone());
            let table = DataTable::new(cols, rows, st.log_key)
                .width(w)
                .max_rows((vp.h / 3).max(3))
                .empty(empty)
                // The page's keys work from the first frame.
                .autofocus()
                .on_action(move |k, id| log_action(pcx, &ca, &sa, Some(k), id))
                .on_activate(move |k| log_action(pcx, &ce, &se, Some(k), "details"))
                .view(gcx, &t);
            col.child(table).build()
        },
    )
}

/// Percent-encode a path segment.
fn urlencode(raw: &str) -> String {
    let mut out = String::new();
    for b in raw.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_matches_known_vectors() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        let long = vec![b'a'; 200];
        assert_eq!(sha256_hex(&long).len(), 64);
    }

    #[test]
    fn kept_token_checks_the_fingerprint() {
        let fp = &sha256_hex(b"tok-1")[..12];
        let d = serde_json::json!({"key": {"fingerprint": fp}});
        assert_eq!(
            kept_token(&d, Some("tok-1")),
            (Some("tok-1".into()), KeyCheck::Match)
        );
        assert_eq!(kept_token(&d, Some("tok-2")), (None, KeyCheck::Stale));
        assert_eq!(kept_token(&d, None), (None, KeyCheck::Missing));
    }

    #[test]
    fn snippet_masks_unless_copied() {
        let d = serde_json::json!({"base_url": "http://h:1/v1", "example_model": "m/x", "access": "token"});
        let shown = snippet("curl", &d, Some("sekret"), false, false);
        assert!(shown.contains(MASK) && !shown.contains("sekret"), "{shown}");
        assert!(snippet("python", &d, Some("sekret"), false, true).contains("api_key=\"sekret\""));
        assert!(snippet("js", &d, None, false, false).contains("YOUR_GATEWAY_TOKEN"));
        let open = serde_json::json!({"base_url": "b", "access": "open"});
        assert!(snippet("curl", &open, None, false, true).contains("not-needed"));
    }

    #[test]
    fn wrap_lines_never_drops_text() {
        let lines = vec![lni(
            "Base URL  http://example.invalid:18781/v1/a/very/long/path/that/wraps",
            Tone::Text,
            10,
        )];
        let out = wrap_lines(&lines, 30);
        let joined: String = out
            .iter()
            .map(|(t, _)| t.trim())
            .collect::<Vec<_>>()
            .join("");
        assert!(
            joined
                .replace(' ', "")
                .contains("http://example.invalid:18781/v1/a/very/long/path/that/wraps"),
            "{out:?}"
        );
    }
}
