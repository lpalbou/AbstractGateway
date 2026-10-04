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

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use abstracttui::app::{ChoiceOutcome, ChoicePrompt};
use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{Phase, UiEvent};
use serde_json::Value;

use super::kit::{InlineConfirm, Row, WrapTable};
use super::util::{line, span, span_bold, wrap_text};
use super::widths::ColRule;
use super::{open_prompt, Ctx};
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

fn ln(text: impl Into<String>, tone: Tone) -> Ln {
    Ln {
        text: text.into(),
        tone,
        indent: 0,
    }
}

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

/// `(•)` / `( )` for a segmented option.
fn seg_mark(on: bool) -> &'static str {
    if on {
        "(•)"
    } else {
        "( )"
    }
}

/// The overview (`oaiStatusCard` + `oaiConnectCard` + `oaiAccessCard`
/// for an admin + `oaiDocsCard`), every sentence the web page's.
pub fn overview_lines(d: &Value, st: &ViewState) -> Vec<Ln> {
    let admin = is_admin(d);
    let busy = st.busy.as_deref();
    let running = b(d, "running");
    let base = s(d, "base_url");
    let mut out: Vec<Ln> = Vec::new();

    // ---- Status
    out.push(ln(
        format!(
            "Status  {}",
            if running {
                "● Running"
            } else {
                "○ Stopped"
            }
        ),
        Tone::Title,
    ));
    out.push(ln(
        "One address for every OpenAI-compatible app.",
        Tone::Muted,
    ));
    out.push(lni(format!("Base URL  {base}"), Tone::Text, 10));
    if !admin {
        out.push(ln(
            format!(
                "{} Only an admin can start or stop it.",
                if running {
                    "Apps can connect now."
                } else {
                    "Stopped: apps can't connect."
                }
            ),
            Tone::Muted,
        ));
    } else {
        let label = if busy == Some("enabled") {
            "Saving..."
        } else {
            "Endpoint"
        };
        out.push(ln(
            format!(
                "{} {label}    x {}    h {}",
                super::switch::marker(b(d, "enabled"), false),
                if busy == Some("restart") {
                    "Restarting..."
                } else {
                    "Restart"
                },
                if busy == Some("check") {
                    "Checking..."
                } else {
                    "Check setup"
                }
            ),
            Tone::Accent,
        ));
        out.push(ln(
            "Endpoint: answers apps at this address. Restart ends open requests.",
            Tone::Muted,
        ));
        if let Some(checks) = &st.checks {
            for c in checks {
                let ok = c.get("ok").and_then(Value::as_bool);
                let (pill, tone) = match ok {
                    Some(true) => ("OK", Tone::Ok),
                    Some(false) => ("Fix", Tone::Err),
                    None => ("Note", Tone::Warn),
                };
                out.push(lni(format!("{pill:<5}{}", s(c, "text")), tone, 5));
            }
        }
    }
    out.push(ln("", Tone::Text));

    // ---- Connect your app
    out.push(ln("Connect your app", Tone::Title));
    out.push(ln(
        "Paste these two values into any OpenAI SDK or app.",
        Tone::Muted,
    ));
    let key = d.get("key").cloned().unwrap_or(Value::Null);
    if key.get("allowed").and_then(Value::as_bool) == Some(false) {
        out.push(ln(
            "The OpenAI API is off for your account. An admin can turn it on in Accounts.",
            Tone::Warn,
        ));
    }
    out.push(lni(format!("Base URL  {base}"), Tone::Text, 10));
    if !b(&key, "own_token") {
        out.push(ln("API key   The gateway admin token", Tone::Text));
        out.push(lni(
            "          The token this gateway was started with.",
            Tone::Muted,
            10,
        ));
    } else {
        let (kept, check) = kept_token(d, st.token.as_deref());
        match kept {
            Some(k) => {
                let shown = if st.reveal { k } else { MASK.to_string() };
                out.push(lni(format!("API key   {shown}"), Tone::Code, 10));
                out.push(lni(
                    "          Your gateway token: apps use it as their API key and act as you.",
                    Tone::Muted,
                    10,
                ));
            }
            None => {
                let why = if check == KeyCheck::Stale {
                    "Your token changed since you signed in here, so this console's copy no longer works."
                } else {
                    "This console doesn't have your token: it connected without one."
                };
                out.push(lni(format!("API key   {MASK}"), Tone::Code, 10));
                out.push(lni(
                    format!("          {why} New key makes one; the gateway shows it once."),
                    Tone::Warn,
                    10,
                ));
            }
        }
    }
    if let Some((tone, text)) = &st.key_notice {
        out.push(ln(text.clone(), *tone));
    }
    if s(d, "access") == "open" {
        out.push(ln(
            "Open mode: SDKs still ask for a key; any text works, for example not-needed.",
            Tone::Muted,
        ));
    }
    out.push(ln(
        "Model names are provider/model, as listed at /v1/models.",
        Tone::Muted,
    ));
    out.push(ln("", Tone::Text));

    // ---- Access (admin)
    if admin {
        out.push(ln("Access", Tone::Title));
        out.push(ln(
            "Changes apply immediately. Who may use the API with their own key: the OpenAI API switch of each account, in Accounts.",
            Tone::Muted,
        ));
        out.push(ln("Authentication  (a switches)", Tone::Text));
        let access = s(d, "access");
        for (id, label, text) in AUTH {
            let shown = if busy == Some(&format!("access:{id}")) {
                "Saving..."
            } else {
                label
            };
            out.push(lni(
                format!("  {} {shown} — {text}", seg_mark(access == id)),
                if access == id {
                    Tone::Accent
                } else {
                    Tone::Text
                },
                6,
            ));
        }
        if access == "open" {
            let current = arr(d, "open_account_options")
                .into_iter()
                .find(|o| b(o, "selected"))
                .map(|o| s(o, "label").to_string())
                .unwrap_or_else(|| s(d, "open_account").to_string());
            let current = if busy == Some("open_account") {
                "Saving...".to_string()
            } else {
                current
            };
            out.push(lni(
                format!("Requests without a key run as  {current}  (u change)"),
                Tone::Text,
                2,
            ));
            out.push(lni(
                "  Guest may use the models (chat, embeddings, speech, images) with no tools, files or attachments. An account brings its own models and providers, and its requests show in its log. Never an admin.",
                Tone::Muted,
                2,
            ));
        }
        out.push(ln("Who can connect  (w changes)", Tone::Text));
        let reach = s(d, "reach");
        for o in arr(d, "reach_options") {
            if o.get("shown").and_then(Value::as_bool) == Some(false) {
                continue;
            }
            let id = s(o, "id");
            let locked = o.get("available").and_then(Value::as_bool) == Some(false);
            let text = if locked && !s(o, "reason").is_empty() {
                s(o, "reason").to_string()
            } else {
                reach_text(id).to_string()
            };
            let label = if busy == Some(&format!("reach:{id}")) {
                "Saving...".to_string()
            } else {
                s(o, "label").to_string()
            };
            let tone = if reach == id {
                Tone::Accent
            } else if locked {
                Tone::Faint
            } else {
                Tone::Text
            };
            out.push(lni(
                format!("  {} {label} — {text}", seg_mark(reach == id)),
                tone,
                6,
            ));
        }
        for w in arr(d, "warnings") {
            let tone = if s(w, "tone") == "warn" {
                Tone::Warn
            } else {
                Tone::Muted
            };
            let extra = if s(w, "id") == "listener" {
                "  (N opens Network)"
            } else {
                ""
            };
            out.push(ln(format!("{}{extra}", s(w, "text")), tone));
        }
        if let Some((tone, text)) = &st.notice {
            out.push(ln(text.clone(), *tone));
        }
        out.push(ln("", Tone::Text));
    }

    // ---- Docs
    let kept = kept_token(d, st.token.as_deref()).0;
    out.push(ln("Docs", Tone::Title));
    out.push(ln(
        format!(
            "What is supported, and a first request with your base URL{}.",
            if kept.is_some() { " and your key" } else { "" }
        ),
        Tone::Muted,
    ));
    out.push(lni(
        format!(
            "OpenAI API compatibility  {} — endpoints and parameters this gateway supports",
            d.pointer("/docs/openai_api")
                .and_then(Value::as_str)
                .unwrap_or("")
        ),
        Tone::Text,
        2,
    ));
    out.push(lni(
        format!(
            "AbstractCore server  {} — the engine behind it",
            d.pointer("/docs/abstractcore")
                .and_then(Value::as_str)
                .unwrap_or("")
        ),
        Tone::Text,
        2,
    ));
    if let Some(sp) = d.get("support") {
        for (label, k, tone) in [
            ("Supported:", "tested", Tone::Ok),
            ("Also served, with an engine set up:", "served", Tone::Text),
            ("Not yet:", "not_yet", Tone::Faint),
        ] {
            let items: Vec<&str> = arr(sp, k).into_iter().filter_map(Value::as_str).collect();
            if !items.is_empty() {
                out.push(lni(format!("{label} {}", items.join(" · ")), tone, 2));
            }
        }
    }
    let tabs: Vec<String> = SNIPPETS
        .iter()
        .enumerate()
        .map(|(i, (_, label))| {
            if i == st.snippet {
                format!("[{label}]")
            } else {
                label.to_string()
            }
        })
        .collect();
    out.push(ln(
        format!("Example  {}  (s next)", tabs.join("  ")),
        Tone::Text,
    ));
    let kind = SNIPPETS[st.snippet.min(SNIPPETS.len() - 1)].0;
    for l in snippet(kind, d, kept.as_deref(), st.reveal, false).lines() {
        out.push(lni(format!("  {l}"), Tone::Code, 4));
    }
    let note = if kept.is_some() {
        "  The key is hidden here; Copy example includes it."
    } else if s(d, "access") == "open" {
        ""
    } else {
        "  Replace YOUR_GATEWAY_TOKEN with your token."
    };
    out.push(ln(format!("c Copy example{note}"), Tone::Muted));
    out
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
        ("Tab", "overview ⇄ requests"),
        ("Enter", "open request"),
        ("b", "copy base URL"),
        ("v", "show/hide key"),
        ("y", "copy key"),
        ("n", "new key"),
    ];
    if !non_admin {
        v.extend([
            ("e", "endpoint"),
            ("x", "restart"),
            ("h", "check setup"),
            ("a", "authentication"),
            ("w", "who can connect"),
            ("u", "run as"),
        ]);
    }
    v.extend([
        ("s", "example"),
        ("c", "copy example"),
        ("f", "full record"),
        ("o", "copy Observer link"),
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

/// A focusable pane of wrapped lines that scrolls (↑/↓, PgUp/PgDn,
/// Home/End) when it holds the keyboard; a ▼/▲ mark says there is more.
fn scroll_pane(
    cx: Scope,
    tt: TokenSet,
    autofocus: bool,
    grow: f32,
    lines: impl Fn() -> Vec<Ln> + 'static,
) -> View {
    let focus = cx.signal(false);
    let top = Rc::new(Cell::new(0i32));
    let page = Rc::new(Cell::new(1i32));
    let tick = cx.signal(0u64);
    let (top_ev, page_ev) = (top.clone(), page.clone());
    let el = Element::new()
        .style(LayoutStyle::default().grow(grow).min_h(3))
        .focusable()
        .focus_signal(focus)
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 {
                    return;
                }
                let pg = page_ev.get().max(1);
                let cur = top_ev.get();
                let next = match k.key {
                    Key::Up => cur - 1,
                    Key::Down => cur + 1,
                    Key::PageUp => cur - pg,
                    Key::PageDown => cur + pg,
                    Key::Home => 0,
                    Key::End => i32::MAX / 2,
                    _ => return,
                };
                top_ev.set(next.max(0));
                tick.update(|t| *t += 1);
                ectx.stop_propagation();
            }
        });
    let el = if autofocus { el.autofocus() } else { el };
    el.child(dyn_view(
        LayoutStyle::default().grow(1.0).min_h(1),
        move || {
            let _ = tick.get();
            let lines = lines();
            let focused = focus.get();
            let (top, page) = (top.clone(), page.clone());
            Element::new()
                .style(LayoutStyle::default().grow(1.0).min_h(1))
                .draw(move |canvas, rect| {
                    if rect.is_empty() {
                        return;
                    }
                    let t = tt;
                    canvas.fill_styled(rect, ' ', &Style::new().fg(t.text).bg(t.surface));
                    // One cell on the right for the scroll mark.
                    let w = (rect.w - 1).max(10) as usize;
                    let all = wrap_lines(&lines, w);
                    let n = all.len() as i32;
                    let tp = top.get().clamp(0, (n - rect.h).max(0));
                    top.set(tp);
                    page.set(rect.h);
                    for (i, (text, tone)) in all
                        .iter()
                        .skip(tp as usize)
                        .take(rect.h as usize)
                        .enumerate()
                    {
                        let mut st = Style::new().fg(tone_ink(&t, *tone)).bg(t.surface);
                        if *tone == Tone::Title {
                            st = st.attrs(Attrs::BOLD);
                        }
                        canvas.print_styled(Point::new(rect.x, rect.y + i as i32), text, &st);
                    }
                    if n > rect.h {
                        let mark = if tp + rect.h >= n { "▲" } else { "▼" };
                        let ink = if focused { t.accent } else { t.text_faint };
                        canvas.print_styled(
                            Point::new(rect.x + rect.w - 1, rect.y + rect.h - 1),
                            mark,
                            &Style::new().fg(ink).bg(t.surface),
                        );
                    }
                })
                .build()
        },
    ))
    .build()
}

/// The page.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let tt = *t;
    let st_reveal = cx.signal(false);
    let st_snippet = cx.signal(0usize);
    let busy: Signal<Option<String>> = cx.signal(None);
    let notice: Signal<Option<(Tone, String)>> = cx.signal(None);
    let key_notice: Signal<Option<(Tone, String)>> = cx.signal(None);
    let checks: Signal<Option<Vec<Value>>> = cx.signal(None);
    let log_sel = cx.signal(0usize);
    let log_open: Signal<Option<usize>> = cx.signal(None);
    let confirm = InlineConfirm::new(cx);
    let last_field: Signal<String> = cx.signal(String::new());

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
    // Opening a row reads its record once.
    {
        let ctx_d = ctx.clone();
        cx.effect(move || {
            let Some(i) = log_open.get() else { return };
            let rid = store.json.get_untracked(KEY_LOGS).ready().and_then(|v| {
                v.get("rows")
                    .and_then(Value::as_array)
                    .and_then(|r| r.get(i))
                    .map(|r| s(r, "request_id").to_string())
            });
            if let Some(rid) = rid.filter(|r| !r.is_empty()) {
                let key = format!("{KEY_LOG_PREFIX}{rid}");
                if !matches!(store.json.get_untracked(&key), Loadable::Ready(_)) {
                    store.json.set(&key, Loadable::Loading);
                    send_get(
                        &ctx_d,
                        &key,
                        &format!("/openai-api/logs/{}", urlencode(&rid)),
                    );
                }
            }
        });
    }
    // The result lines also go to the footer's notice lane: the card that
    // carries them may be scrolled out of the overview pane.
    cx.effect(move || {
        if let Some((_, text)) = notice.get() {
            store.notice.set(Some(text));
        }
    });
    cx.effect(move || {
        if let Some((_, text)) = key_notice.get() {
            store.notice.set(Some(text));
        }
    });
    // Write outcomes → the web page's sentences.
    {
        let ctx_w = ctx.clone();
        cx.effect(move || {
            if let Some(w) = store.json.write(KEY_CHANGE) {
                if !w.is_pending() {
                    let field = last_field.get_untracked();
                    match w {
                        WriteState::Done(v) => {
                            notice.set(Some((Tone::Ok, saved_text(&field, &v))))
                        }
                        WriteState::Failed(e) => {
                            notice.set(Some((Tone::Err, format!("Not saved: {}", e.message))))
                        }
                        WriteState::Pending => {}
                    }
                    busy.set(None);
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
                            notice.set(Some((Tone::Ok, format!("Restarted: {what}."))));
                        }
                        WriteState::Failed(e) => notice
                            .set(Some((Tone::Err, format!("Not restarted: {}", e.message)))),
                        WriteState::Pending => {}
                    }
                    busy.set(None);
                    store.json.set_write(KEY_RESTART, None);
                }
            }
            if let Some(w) = store.json.write(KEY_CHECK) {
                if !w.is_pending() {
                    match w {
                        WriteState::Done(v) => checks.set(Some(
                            v.get("checks")
                                .and_then(Value::as_array)
                                .cloned()
                                .unwrap_or_default(),
                        )),
                        WriteState::Failed(e) => checks.set(Some(vec![serde_json::json!({
                            "id": "error", "ok": false, "text": e.message
                        })])),
                        WriteState::Pending => {}
                    }
                    busy.set(None);
                    store.json.set_write(KEY_CHECK, None);
                }
            }
            if let Some(w) = store.json.write(KEY_NEW_KEY) {
                if !w.is_pending() {
                    match w {
                        WriteState::Done(v) => {
                            let token = s(&v, "token").to_string();
                            if token.is_empty() {
                                key_notice.set(Some((
                                    Tone::Err,
                                    "No new key: The gateway answered without the new key."
                                        .into(),
                                )));
                            } else {
                                // The console now signs in with the new key
                                // (the old one stopped working).
                                ctx_w.ui.conn_token.set(token);
                                st_reveal.set(true);
                                key_notice.set(Some((
                                    Tone::Ok,
                                    "New key made: your old one stopped working. Copy it now: the gateway shows a key only once.".into(),
                                )));
                                ctx_w.connect_now();
                            }
                        }
                        WriteState::Failed(e) => key_notice
                            .set(Some((Tone::Err, format!("No new key: {}", e.message)))),
                        WriteState::Pending => {}
                    }
                    busy.set(None);
                    store.json.set_write(KEY_NEW_KEY, None);
                }
            }
        });
    }

    let doc = move || store.json.get_untracked(KEY_PAGE).ready().cloned();
    let token_now = {
        let ctx_t = ctx.clone();
        move || ctx_t.effective_credentials().1
    };

    // ---- Keys
    let mut root = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    let say = move |m: &str| store.notice.set(Some(m.to_string()));
    let admin_only = move |what: &str| -> bool {
        match doc() {
            Some(d) if is_admin(&d) => true,
            Some(_) => {
                store.notice.set(Some(format!("Only an admin can {what}.")));
                false
            }
            None => {
                store
                    .notice
                    .set(Some("Reading the OpenAI API settings...".into()));
                false
            }
        }
    };
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            if admin_only("start or stop it") {
                let on = doc().map(|d| b(&d, "enabled")).unwrap_or(false);
                last_field.set("enabled".into());
                notice.set(None);
                change(&c, busy, "enabled", Value::Bool(!on));
            }
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            if !admin_only("start or stop it") || busy.get_untracked().is_some() {
                return;
            }
            if !doc().map(|d| b(&d, "enabled")).unwrap_or(false) {
                say("Restart needs the endpoint on (e).");
                return;
            }
            busy.set(Some("restart".into()));
            notice.set(None);
            post(&c, KEY_RESTART, PATH_RESTART, "OpenAI API: restart", false);
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('h')), move |_| {
            if !admin_only("check the setup") || busy.get_untracked().is_some() {
                return;
            }
            busy.set(Some("check".into()));
            post(&c, KEY_CHECK, PATH_CHECK, "OpenAI API: check setup", true);
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('a')), move |_| {
            if !admin_only("change the authentication") {
                return;
            }
            let next = if doc().map(|d| s(&d, "access") == "open").unwrap_or(false) {
                "token"
            } else {
                "open"
            };
            last_field.set("access".into());
            notice.set(None);
            change(&c, busy, "access", Value::String(next.into()));
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('w')), move |_| {
            if !admin_only("change who can connect") || busy.get_untracked().is_some() {
                return;
            }
            let Some(d) = doc() else { return };
            let mut prompt = ChoicePrompt::new("Who can connect");
            let mut initial = String::new();
            for o in arr(&d, "reach_options") {
                if o.get("shown").and_then(Value::as_bool) == Some(false) {
                    continue;
                }
                let id = s(o, "id").to_string();
                let locked = o.get("available").and_then(Value::as_bool) == Some(false);
                let text = if locked && !s(o, "reason").is_empty() {
                    s(o, "reason").to_string()
                } else {
                    reach_text(&id).to_string()
                };
                if b(o, "selected") {
                    initial = id.clone();
                }
                prompt = prompt.option_detail(id, s(o, "label").to_string(), text);
            }
            if !initial.is_empty() {
                prompt = prompt.initial(initial.clone());
            }
            let c2 = c.clone();
            let d2 = d.clone();
            open_prompt(cx, c.ui, prompt, move |out| {
                let ChoiceOutcome::Answered(a) = out else {
                    return;
                };
                let Some(id) = a.selected.first().cloned() else {
                    return;
                };
                if id == initial {
                    return;
                }
                let locked = arr(&d2, "reach_options")
                    .into_iter()
                    .find(|o| s(o, "id") == id)
                    .filter(|o| o.get("available").and_then(Value::as_bool) == Some(false))
                    .map(|o| s(o, "reason").to_string());
                if let Some(reason) = locked {
                    notice.set(Some((Tone::Warn, reason)));
                    return;
                }
                last_field.set("reach".into());
                notice.set(None);
                change(&c2, busy, "reach", Value::String(id));
            });
        });
    }
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('u')), move |_| {
            if !admin_only("choose who requests without a key run as")
                || busy.get_untracked().is_some()
            {
                return;
            }
            let Some(d) = doc() else { return };
            if s(&d, "access") != "open" {
                say("Requests without a key exist in Open mode only (a switches).");
                return;
            }
            let mut prompt = ChoicePrompt::new("Requests without a key run as");
            let mut initial = String::new();
            for o in arr(&d, "open_account_options") {
                let id = s(o, "id").to_string();
                let label = if o.get("available").and_then(Value::as_bool) == Some(false) {
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
                if b(o, "selected") {
                    initial = id.clone();
                }
                prompt = prompt.option(id, label);
            }
            if !initial.is_empty() {
                prompt = prompt.initial(initial.clone());
            }
            let c2 = c.clone();
            let d2 = d.clone();
            open_prompt(cx, c.ui, prompt, move |out| {
                let ChoiceOutcome::Answered(a) = out else {
                    return;
                };
                let Some(id) = a.selected.first().cloned() else {
                    return;
                };
                if id == initial {
                    return;
                }
                let locked = arr(&d2, "open_account_options")
                    .into_iter()
                    .find(|o| s(o, "id") == id)
                    .filter(|o| o.get("available").and_then(Value::as_bool) == Some(false))
                    .map(|o| s(o, "reason").to_string());
                if let Some(reason) = locked {
                    notice.set(Some((Tone::Warn, reason)));
                    return;
                }
                last_field.set("open_account".into());
                notice.set(None);
                change(&c2, busy, "open_account", Value::String(id));
            });
        });
    }
    root = root.shortcut(KeyChord::plain(Key::Char('v')), move |_| {
        st_reveal.update(|r| *r = !*r);
    });
    {
        let tk = token_now.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('y')), move |_| {
            let Some(d) = doc() else { return };
            match kept_token(&d, tk().as_deref()).0 {
                Some(k) => {
                    copy_to_clipboard(k);
                    say("API key copied to the clipboard");
                }
                None => say("No key to copy: New key (n) makes one."),
            }
        });
    }
    root = root.shortcut(KeyChord::plain(Key::Char('b')), move |_| {
        if let Some(d) = doc() {
            let u = s(&d, "base_url").to_string();
            copy_to_clipboard(u.clone());
            say(&format!("copied {u}"));
        }
    });
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('n')), move |_| {
            if confirm.is_open() || busy.get_untracked().is_some() {
                return;
            }
            let c2 = c.clone();
            confirm.ask(NEW_KEY_SENTENCE, "New key", move || {
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
            });
        });
    }
    root = root.shortcut(KeyChord::plain(Key::Char('s')), move |_| {
        st_snippet.update(|i| *i = (*i + 1) % SNIPPETS.len());
    });
    {
        let tk = token_now.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            let Some(d) = doc() else { return };
            let kept = kept_token(&d, tk().as_deref()).0;
            let kind = SNIPPETS[st_snippet.get_untracked() % SNIPPETS.len()].0;
            copy_to_clipboard(snippet(kind, &d, kept.as_deref(), false, true));
            say("example copied to the clipboard");
        });
    }
    root = root.shortcut(KeyChord::plain(Key::Char('o')), move |_| {
        let rows = store.json.get_untracked(KEY_LOGS);
        let path = rows.ready().and_then(|v| {
            v.get("rows")
                .and_then(Value::as_array)
                .and_then(|r| r.get(log_sel.get_untracked()))
                .map(|r| s(r, "observer_path").to_string())
        });
        match path.filter(|p| !p.is_empty()) {
            Some(p) => {
                let url = observer_url(doc().as_ref(), &p);
                copy_to_clipboard(url.clone());
                say(&format!("copied {url}"));
            }
            None => say("Not part of a run: Observer shows the requests that a run made."),
        }
    });
    // `f`: the selected request's full record in an overlay that scrolls
    // (a long JSON body does not fit the table's opened row).
    {
        let c = ctx.clone();
        root = root.shortcut(KeyChord::plain(Key::Char('f')), move |_| {
            let row = store.json.get_untracked(KEY_LOGS).ready().and_then(|v| {
                v.get("rows")
                    .and_then(Value::as_array)
                    .and_then(|r| r.get(log_sel.get_untracked()))
                    .cloned()
            });
            let Some(row) = row else {
                say("No requests yet.");
                return;
            };
            let rid = s(&row, "request_id").to_string();
            let key = format!("{KEY_LOG_PREFIX}{rid}");
            if !matches!(store.json.get_untracked(&key), Loadable::Ready(_)) {
                store.json.set(&key, Loadable::Loading);
                send_get(&c, &key, &format!("/openai-api/logs/{}", urlencode(&rid)));
            }
            let title = format!(
                "Request {} · {}",
                local_time(s(&row, "ts")),
                log_cells(&row)[1]
            );
            super::kit::open_overlay(
                &c,
                cx,
                title,
                &[
                    ("↑↓ PgUp/PgDn", "scroll"),
                    ("i", "copy request"),
                    ("p", "copy response"),
                ],
                move |mcx, _close, _guard| {
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
                    let pane = scroll_pane(mcx, tt, true, 1.0, move || {
                        let d = store.json.get_untracked(KEY_PAGE).ready().cloned();
                        detail_lines(&store.json.get(&key), d.as_ref())
                            .into_iter()
                            .map(|l| ln(l, Tone::Text))
                            .collect()
                    });
                    Element::new()
                        .style(LayoutStyle::column().grow(1.0))
                        .shortcut(KeyChord::plain(Key::Char('i')), move |_| copy("request"))
                        .shortcut(KeyChord::plain(Key::Char('p')), move |_| copy2("response"))
                        .child(pane)
                        .build()
                },
            );
        });
    }
    let root = confirm.keys(root);

    // ---- The overview pane (scrolls when focused)
    let vp = abstracttui::app::use_viewport(cx);
    let overview = {
        let tk = token_now.clone();
        scroll_pane(cx, tt, true, 3.0, move || {
            let page_doc = store.json.get(KEY_PAGE);
            let st = ViewState {
                token: tk(),
                reveal: st_reveal.get(),
                snippet: st_snippet.get(),
                busy: busy.get(),
                notice: notice.get(),
                key_notice: key_notice.get(),
                checks: checks.get(),
            };
            if !store.conn.with(ConnPhase::is_connected) {
                return vec![ln(
                    "not connected — probe the gateway on 1 Connection first",
                    Tone::Faint,
                )];
            }
            match &page_doc {
                Loadable::Ready(d) => overview_lines(d, &st),
                Loadable::Failed(e) => vec![
                    ln("Could not read the OpenAI API settings.", Tone::Err),
                    ln(e.message.clone(), Tone::Err),
                    ln("r Try again", Tone::Muted),
                ],
                _ => vec![ln("Reading the OpenAI API settings...", Tone::Muted)],
            }
        })
    };

    // ---- Recent requests
    let logs_held = Rc::new(Cell::new(false));
    let logs_head = dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
        let t = tt;
        let w = (vp.get().w - 4).max(20) as usize;
        let logs = store.json.get(KEY_LOGS);
        let scope = logs
            .ready()
            .map(|v| s(v, "scope").to_string())
            .unwrap_or_default();
        let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
        col = col.child(line(vec![span_bold("Recent requests", t.text)]));
        for l in wrap_text(&logs_note(&scope), w) {
            col = col.child(line(vec![span(l, t.text_muted)]));
        }
        if let Loadable::Failed(e) = &logs {
            col = col.child(line(vec![span(
                format!("Could not read the request log. {}", e.message),
                t.error,
            )]));
        }
        col.build()
    });
    let logs_table = {
        let held = logs_held.clone();
        dyn_view(LayoutStyle::default().grow(2.0).min_h(3), move || {
            let t = tt;
            let logs = store.json.get(KEY_LOGS);
            let page_doc = store.json.get(KEY_PAGE).ready().cloned();
            let rows_v: Option<Vec<Value>> = match &logs {
                Loadable::Ready(v) => Some(
                    v.get("rows")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default(),
                ),
                _ => None,
            };
            let open = log_open.get();
            let rows: Vec<Row> = rows_v
                .clone()
                .unwrap_or_default()
                .iter()
                .enumerate()
                .map(|(i, r)| {
                    let detail = if open == Some(i) {
                        let key = format!("{KEY_LOG_PREFIX}{}", s(r, "request_id"));
                        detail_lines(&store.json.get(&key), page_doc.as_ref())
                    } else {
                        vec!["Reading the request...".into()]
                    };
                    let status = r.get("status").and_then(Value::as_i64).unwrap_or(0);
                    Row::new(log_cells(r))
                        .detail(detail)
                        .dim(!(200..300).contains(&status))
                })
                .collect();
            let empty = if rows_v.is_some() {
                "No requests yet."
            } else {
                "Reading the log..."
            };
            let el = WrapTable::new(log_rules(), rows, log_sel)
                .expanded(log_open)
                .empty(empty)
                .layout(LayoutStyle::default().grow(1.0).min_h(2))
                .element(cx, &t);
            let (h1, h2) = (held.clone(), held.clone());
            let want = held.get();
            let el = el.on(Phase::Bubble, move |_c, ev| match ev {
                UiEvent::FocusIn => h1.set(true),
                UiEvent::FocusOut => h2.set(false),
                _ => {}
            });
            if want {
                el.autofocus().build()
            } else {
                el.build()
            }
        })
    };

    let page_body = root
        .child(overview)
        .child(dyn_view(
            LayoutStyle::column().gap(0).shrink(0.0),
            move || confirm.view(&tt, vp.get().w - 4),
        ))
        .child(logs_head)
        .child(logs_table)
        .build();

    Block::new()
        .border(BorderKind::Rounded)
        .title("OpenAI API — one address for every OpenAI-compatible app")
        .fill(t.surface)
        .layout(
            LayoutStyle::column()
                .gap(0)
                .grow(1.0)
                .padding(Edges::hv(1, 0))
                .clip(),
        )
        .child(page_body)
        .element(t)
        .build()
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
