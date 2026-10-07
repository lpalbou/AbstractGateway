//! The workspace chooser (round 14, R14.3 — DESIGN "R11.1 FINAL"): the
//! terminal twin of the kit's `WorkspaceChooser`, at the two levels the
//! gateway console uses on its Accounts page.
//!
//! - **gateway** — "Eligible workspaces" (admins; `E` on Accounts):
//!   `GET/PUT /workspace/policy`. The posture ("Deny everything, allow
//!   listed workspaces" | "Allow everything, refuse listed workspaces"),
//!   Everything else under the second posture, and rows whose mode is the
//!   CAP every account stays under (Read-only | Read & write | Refused);
//!   the built-in refusals are listed, fixed.
//! - **account** — "Workspaces — <id>" (`w` on every Accounts row the
//!   gateway marks `actions.workspace` available: humans, entities, the
//!   admin's own): `GET/PUT /workspace/policy/{tenant:id | me}`. The
//!   gateway line on top ("Gateway: <gateway_summary>", verbatim), "Follow
//!   the gateway policy" (configured:false), the account's own posture and
//!   rows — a mode above the gateway's cap stays visible and is refused
//!   with the kit's sentence — and the add row.
//!
//! Like the kit, this module holds NO policy logic: no path check, no
//! clamp, no cap computation. Caps are the gateway's
//! (`effective.folders[].cap`); the effective line and the gateway line
//! are the gateway's own strings, printed verbatim. Every change is ONE
//! PUT of the whole level (the kit's payload builders, mirrored below); a
//! refusal shows the gateway's sentence + "Not saved."; no Save button.
//!
//! The words are the kit's ONE wording table (`WORKSPACE_CHOOSER_TEXT` in
//! abstractuic `ui-kit/src/workspace_chooser_core.ts`), copied byte for
//! byte into [`TABLE`]; `cargo test` diffs it against the recorded kit
//! table (`tests/fixtures/r14w3_kit_workspace_chooser_text.json`) and
//! `scripts/check_workspace_wording.py` diffs that fixture against the
//! live kit file.

use abstracttui::prelude::*;

use serde_json::{json, Value};

use super::Ctx;
use crate::api::{ApiError, ApiErrorKind};
use crate::store::json::WriteState;
use crate::store::Loadable;
use crate::worker::json::JsonCmd;
use crate::worker::Cmd;

// ---------------------------------------------------------------------------
// The kit's wording table (byte for byte)
// ---------------------------------------------------------------------------

/// The kit's `WORKSPACE_CHOOSER_TEXT`, one constant per key.
pub mod text {
    pub const TITLE: &str = "Workspaces";
    pub const GATEWAY_TITLE: &str = "Eligible workspaces";
    pub const GATEWAY_HELP: &str =
        "The workspaces accounts may choose from, and the most each one allows.";
    pub const ACCOUNT_HELP: &str =
        "The workspaces this account's agents use, among the eligible ones.";
    pub const SESSION_HELP: &str =
        "The workspaces this conversation uses, among the eligible ones.";
    pub const RUN_HELP: &str = "The workspaces this run uses, among the eligible ones.";
    pub const GATEWAY_PREFIX: &str = "Gateway:";
    pub const POSTURE_LABEL: &str = "Workspaces agents may use";
    pub const POSTURE_ALLOWED_ONLY: &str = "Deny everything, allow listed workspaces";
    pub const POSTURE_ALLOWED_ONLY_HELP: &str = "Agents may only work in the listed workspaces.";
    pub const POSTURE_ANY_EXCEPT_DENIED: &str = "Allow everything, refuse listed workspaces";
    pub const POSTURE_ANY_EXCEPT_DENIED_HELP: &str =
        "Agents may work in any workspace except the refused ones.";
    pub const ACCESS_LABEL: &str = "Permission";
    pub const ACCESS_READ: &str = "Read-only";
    pub const ACCESS_READ_WRITE: &str = "Read & write";
    pub const ACCESS_DENIED: &str = "Refused";
    pub const CAP_READ_ONLY: &str = "The gateway allows this workspace read-only";
    pub const CAP_REFUSED: &str = "The gateway refuses this workspace";
    pub const EVERYTHING_ELSE: &str = "Everything else";
    pub const ALLOWED_TITLE: &str = "Allowed workspaces";
    pub const DENIED_TITLE: &str = "Refused workspaces";
    pub const BUILTIN_REFUSED: &str = "Always refused: the gateway's own data and credentials";
    pub const EMPTY_ALLOWED: &str =
        "No workspace is listed: agents only use their private workspace.";
    pub const PRIVATE_NOTE: &str =
        "The private workspace of each run is always available, read & write.";
    pub const ADD_PLACEHOLDER: &str = "Add a workspace path";
    pub const ADD: &str = "Add";
    pub const CHOOSE: &str = "Choose…";
    pub const REMOVE: &str = "Remove";
    pub const FOLLOW_GATEWAY: &str = "Follow the gateway policy";
    pub const FOLLOW_GATEWAY_HELP: &str = "On: this account gets exactly what the gateway allows.";
    pub const USE_DEFAULT: &str = "Use my default";
    pub const USE_DEFAULT_HELP: &str = "On: the account's default workspaces apply.";
    pub const LOCKED: &str = "These workspaces can be seen here but not changed.";
    pub const LOADING: &str = "Loading…";
    pub const SAVED: &str = "Saved";
    pub const NOT_SAVED: &str = "Not saved.";
}

/// The whole table, in the kit's key order (the parity tests diff this).
pub const TABLE: [(&str, &str); 36] = [
    ("title", text::TITLE),
    ("gatewayTitle", text::GATEWAY_TITLE),
    ("gatewayHelp", text::GATEWAY_HELP),
    ("accountHelp", text::ACCOUNT_HELP),
    ("sessionHelp", text::SESSION_HELP),
    ("runHelp", text::RUN_HELP),
    ("gatewayPrefix", text::GATEWAY_PREFIX),
    ("postureLabel", text::POSTURE_LABEL),
    ("postureAllowedOnly", text::POSTURE_ALLOWED_ONLY),
    ("postureAllowedOnlyHelp", text::POSTURE_ALLOWED_ONLY_HELP),
    ("postureAnyExceptDenied", text::POSTURE_ANY_EXCEPT_DENIED),
    (
        "postureAnyExceptDeniedHelp",
        text::POSTURE_ANY_EXCEPT_DENIED_HELP,
    ),
    ("accessLabel", text::ACCESS_LABEL),
    ("accessRead", text::ACCESS_READ),
    ("accessReadWrite", text::ACCESS_READ_WRITE),
    ("accessDenied", text::ACCESS_DENIED),
    ("capReadOnly", text::CAP_READ_ONLY),
    ("capRefused", text::CAP_REFUSED),
    ("everythingElse", text::EVERYTHING_ELSE),
    ("allowedTitle", text::ALLOWED_TITLE),
    ("deniedTitle", text::DENIED_TITLE),
    ("builtinRefused", text::BUILTIN_REFUSED),
    ("emptyAllowed", text::EMPTY_ALLOWED),
    ("privateNote", text::PRIVATE_NOTE),
    ("addPlaceholder", text::ADD_PLACEHOLDER),
    ("add", text::ADD),
    ("choose", text::CHOOSE),
    ("remove", text::REMOVE),
    ("followGateway", text::FOLLOW_GATEWAY),
    ("followGatewayHelp", text::FOLLOW_GATEWAY_HELP),
    ("useDefault", text::USE_DEFAULT),
    ("useDefaultHelp", text::USE_DEFAULT_HELP),
    ("locked", text::LOCKED),
    ("loading", text::LOADING),
    ("saved", text::SAVED),
    ("notSaved", text::NOT_SAVED),
];

use text as T;

// ---------------------------------------------------------------------------
// The model (parsed strictly, like the kit's workspaceAsState)
// ---------------------------------------------------------------------------

/// Read-only, Read & write, or Refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Rw,
    Ro,
    Deny,
}

impl Mode {
    pub fn id(self) -> &'static str {
        match self {
            Mode::Rw => "rw",
            Mode::Ro => "ro",
            Mode::Deny => "deny",
        }
    }
    pub fn parse(s: &str) -> Option<Mode> {
        match s {
            "rw" => Some(Mode::Rw),
            "ro" => Some(Mode::Ro),
            "deny" => Some(Mode::Deny),
            _ => None,
        }
    }
    /// The kit's `workspaceModeLabel`.
    pub fn label(self) -> &'static str {
        match self {
            Mode::Ro => T::ACCESS_READ,
            Mode::Deny => T::ACCESS_DENIED,
            Mode::Rw => T::ACCESS_READ_WRITE,
        }
    }
    fn order(self) -> u8 {
        match self {
            Mode::Deny => 0,
            Mode::Ro => 1,
            Mode::Rw => 2,
        }
    }
}

/// The modes a row offers, left to right (the kit's segmented order).
pub const MODES: [Mode; 3] = [Mode::Rw, Mode::Ro, Mode::Deny];
/// Everything else (posture b): Read & write | Read-only.
pub const ACCESS: [Mode; 2] = [Mode::Rw, Mode::Ro];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Posture {
    AllowedOnly,
    AnyExceptDenied,
}

impl Posture {
    pub fn id(self) -> &'static str {
        match self {
            Posture::AllowedOnly => "allowed_only",
            Posture::AnyExceptDenied => "any_except_denied",
        }
    }
    pub fn parse(s: &str) -> Option<Posture> {
        match s {
            "allowed_only" => Some(Posture::AllowedOnly),
            "any_except_denied" => Some(Posture::AnyExceptDenied),
            _ => None,
        }
    }
    /// The kit's `workspacePostureLabel`.
    pub fn label(self) -> &'static str {
        match self {
            Posture::AnyExceptDenied => T::POSTURE_ANY_EXCEPT_DENIED,
            Posture::AllowedOnly => T::POSTURE_ALLOWED_ONLY,
        }
    }
    /// The kit's `workspacePostureHelp`.
    pub fn help(self) -> &'static str {
        match self {
            Posture::AnyExceptDenied => T::POSTURE_ANY_EXCEPT_DENIED_HELP,
            Posture::AllowedOnly => T::POSTURE_ALLOWED_ONLY_HELP,
        }
    }
}

/// The two postures, left to right.
pub const POSTURES: [Posture; 2] = [Posture::AllowedOnly, Posture::AnyExceptDenied];

/// The chooser levels this console uses.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Gateway,
    Account,
}

impl Level {
    /// The kit's `workspaceLevelHelp`.
    pub fn help(self) -> &'static str {
        match self {
            Level::Gateway => T::GATEWAY_HELP,
            Level::Account => T::ACCOUNT_HELP,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rule {
    pub path: String,
    pub mode: Mode,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Policy {
    pub account: Option<String>,
    /// Account level only.
    pub configured: Option<bool>,
    pub posture: Posture,
    /// The mode of everything not listed (posture b): Ro or Rw.
    pub default_mode: Mode,
    pub folders: Vec<Rule>,
    /// Gateway level, admins only.
    pub builtin_refused: Vec<String>,
    /// Gateway level: the ceiling line.
    pub summary: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EffRow {
    pub path: String,
    pub mode: Mode,
    pub cap: Mode,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Effective {
    pub posture: Posture,
    /// None under "Deny everything…" when the gateway answers null.
    pub default_mode: Option<Mode>,
    pub folders: Vec<EffRow>,
    pub summary: String,
    pub gateway_summary: String,
}

/// Everything the chooser renders (the kit's `WorkspaceChooserState`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    pub policy: Policy,
    pub effective: Option<Effective>,
    pub can_edit: Option<bool>,
}

/// The kit's OLD_MODEL sentence.
pub const OLD_MODEL: &str =
    "The gateway answered with an older workspace model (it needs the round-11 workspace API).";

fn is_access(m: Mode) -> bool {
    matches!(m, Mode::Ro | Mode::Rw)
}

/// The `policy` of an answer, checked (the kit's `workspaceAsPolicy`:
/// fails loudly on a missing field or an older model).
pub fn as_policy(value: &Value, level: Level) -> Result<Policy, String> {
    let empty = json!({});
    let v = value
        .get("policy")
        .filter(|p| p.is_object())
        .unwrap_or(&empty);
    if v.get("shared_workspace").is_some() || value.get("shared_workspace").is_some() {
        return Err(OLD_MODEL.into());
    }
    let need = match level {
        Level::Gateway => "summary",
        Level::Account => "configured",
    };
    let fail = || {
        format!("The gateway answered without a workspace policy (policy.posture, default_mode, folders, {need}). {OLD_MODEL}")
    };
    let posture = v
        .get("posture")
        .and_then(Value::as_str)
        .and_then(Posture::parse)
        .ok_or_else(fail)?;
    let default_mode = v
        .get("default_mode")
        .and_then(Value::as_str)
        .and_then(Mode::parse)
        .filter(|m| is_access(*m))
        .ok_or_else(fail)?;
    let mut folders = Vec::new();
    for r in v
        .get("folders")
        .and_then(Value::as_array)
        .ok_or_else(fail)?
    {
        let path = r.get("path").and_then(Value::as_str).ok_or_else(fail)?;
        let mode = r
            .get("mode")
            .and_then(Value::as_str)
            .and_then(Mode::parse)
            .ok_or_else(fail)?;
        folders.push(Rule {
            path: path.to_string(),
            mode,
        });
    }
    let (summary, configured) = match level {
        Level::Gateway => (
            Some(
                v.get("summary")
                    .and_then(Value::as_str)
                    .ok_or_else(fail)?
                    .to_string(),
            ),
            None,
        ),
        Level::Account => (
            None,
            Some(
                v.get("configured")
                    .and_then(Value::as_bool)
                    .ok_or_else(fail)?,
            ),
        ),
    };
    let builtin_refused = match v.get("builtin_refused") {
        None => Vec::new(),
        Some(b) => b
            .as_array()
            .ok_or_else(fail)?
            .iter()
            .map(|p| p.as_str().map(str::to_string).ok_or_else(fail))
            .collect::<Result<Vec<_>, _>>()?,
    };
    Ok(Policy {
        account: v.get("account").and_then(Value::as_str).map(str::to_string),
        configured,
        posture,
        default_mode,
        folders,
        builtin_refused,
        summary,
    })
}

/// An effective answer, checked (the kit's `workspaceAsEffective`).
pub fn as_effective(v: &Value) -> Result<Effective, String> {
    if v.get("shared_workspace").is_some() {
        return Err(OLD_MODEL.into());
    }
    let fail = || {
        format!("The gateway answered without the effective workspaces (posture, default_mode, folders with cap, summary, gateway_summary). {OLD_MODEL}")
    };
    let posture = v
        .get("posture")
        .and_then(Value::as_str)
        .and_then(Posture::parse)
        .ok_or_else(fail)?;
    let default_mode = match v.get("default_mode") {
        Some(Value::Null) if posture == Posture::AllowedOnly => None,
        Some(Value::String(s)) => Some(Mode::parse(s).filter(|m| is_access(*m)).ok_or_else(fail)?),
        _ => return Err(fail()),
    };
    let summary = v.get("summary").and_then(Value::as_str).ok_or_else(fail)?;
    let gateway_summary = v
        .get("gateway_summary")
        .and_then(Value::as_str)
        .ok_or_else(fail)?;
    let mut folders = Vec::new();
    for f in v
        .get("folders")
        .and_then(Value::as_array)
        .ok_or_else(fail)?
    {
        let m = |k: &str| {
            f.get(k)
                .and_then(Value::as_str)
                .and_then(Mode::parse)
                .ok_or_else(fail)
        };
        folders.push(EffRow {
            path: f
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(fail)?
                .to_string(),
            mode: m("mode")?,
            cap: m("cap")?,
        });
    }
    Ok(Effective {
        posture,
        default_mode,
        folders,
        summary: summary.to_string(),
        gateway_summary: gateway_summary.to_string(),
    })
}

/// A level's GET/PUT answer → the chooser state (the kit's `workspaceAsState`).
pub fn as_state(value: &Value, level: Level) -> Result<State, String> {
    let policy = as_policy(value, level)?;
    let effective = match level {
        Level::Gateway => None,
        Level::Account => Some(as_effective(
            value.get("effective").unwrap_or(&Value::Null),
        )?),
    };
    Ok(State {
        policy,
        effective,
        can_edit: value.get("can_edit").and_then(Value::as_bool),
    })
}

/// One row as shown (the kit's `WorkspaceChooserRow`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ViewRow {
    pub path: String,
    pub mode: Mode,
    /// The gateway's cap for this path; None = gateway level / unknown.
    pub cap: Option<Mode>,
    /// Modes that can be picked here.
    pub allowed: Vec<Mode>,
    /// Why a mode cannot be picked (per mode).
    pub reasons: Vec<(Mode, &'static str)>,
    pub builtin: bool,
    pub editable: bool,
}

impl ViewRow {
    pub fn reason(&self, m: Mode) -> Option<&'static str> {
        self.reasons.iter().find(|(x, _)| *x == m).map(|(_, r)| *r)
    }
}

/// What the chooser shows for one level (the kit's `WorkspaceChooserView`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChooserView {
    pub level: Level,
    pub posture: Posture,
    /// Account level: configured:false (following the gateway).
    pub following: bool,
    pub rows: Vec<ViewRow>,
    /// Posture b: the mode of everything not listed, and whether it can change.
    pub everything_else: Option<(Mode, bool)>,
    pub can_add: bool,
    /// "Gateway: <gateway_summary>" (account level).
    pub gateway_line: Option<String>,
    /// The effective line, verbatim.
    pub summary: String,
    pub locked: bool,
}

impl ChooserView {
    pub fn allowed_rows(&self) -> Vec<&ViewRow> {
        self.rows.iter().filter(|r| r.mode != Mode::Deny).collect()
    }
    pub fn refused_rows(&self) -> Vec<&ViewRow> {
        self.rows.iter().filter(|r| r.mode == Mode::Deny).collect()
    }
}

/// The kit's `workspaceModesUnderCap`.
pub fn modes_under_cap(cap: Option<Mode>) -> (Vec<Mode>, Vec<(Mode, &'static str)>) {
    let Some(cap) = cap else {
        return (MODES.to_vec(), Vec::new());
    };
    let reason = if cap == Mode::Deny {
        T::CAP_REFUSED
    } else {
        T::CAP_READ_ONLY
    };
    let allowed: Vec<Mode> = MODES
        .iter()
        .copied()
        .filter(|m| m.order() <= cap.order())
        .collect();
    let reasons = MODES
        .iter()
        .copied()
        .filter(|m| !allowed.contains(m))
        .map(|m| (m, reason))
        .collect();
    (allowed, reasons)
}

/// The kit's `workspaceChooserView`.
pub fn chooser_view(level: Level, state: &State) -> Result<ChooserView, String> {
    let policy = &state.policy;
    match level {
        Level::Gateway if policy.summary.is_none() => {
            return Err("The gateway workspace policy has no summary line.".into())
        }
        Level::Account if state.effective.is_none() => {
            return Err(
                "A account-level workspace chooser needs the gateway's effective workspaces."
                    .into(),
            )
        }
        _ => {}
    }
    let following = level != Level::Gateway && policy.configured == Some(false);
    let locked = state.can_edit == Some(false);
    let caps: Vec<(String, Mode)> = state
        .effective
        .as_ref()
        .map(|e| e.folders.iter().map(|f| (f.path.clone(), f.cap)).collect())
        .unwrap_or_default();
    let (source, posture, default_mode) = match (&state.effective, following) {
        (Some(e), true) => (
            e.folders
                .iter()
                .map(|f| Rule {
                    path: f.path.clone(),
                    mode: f.mode,
                })
                .collect::<Vec<_>>(),
            e.posture,
            e.default_mode.unwrap_or(policy.default_mode),
        ),
        _ => (policy.folders.clone(), policy.posture, policy.default_mode),
    };
    let editable = !following && !locked;
    let mut rows: Vec<ViewRow> = source
        .iter()
        .map(|r| {
            let cap = match level {
                Level::Gateway => None,
                Level::Account => caps.iter().find(|(p, _)| *p == r.path).map(|(_, c)| *c),
            };
            let (allowed, reasons) = modes_under_cap(cap);
            ViewRow {
                path: r.path.clone(),
                mode: r.mode,
                cap,
                allowed,
                reasons,
                builtin: false,
                editable,
            }
        })
        .collect();
    if level == Level::Gateway {
        for p in &policy.builtin_refused {
            rows.push(ViewRow {
                path: p.clone(),
                mode: Mode::Deny,
                cap: None,
                allowed: Vec::new(),
                reasons: Vec::new(),
                builtin: true,
                editable: false,
            });
        }
    }
    let eff = state.effective.as_ref();
    Ok(ChooserView {
        level,
        posture,
        following,
        rows,
        everything_else: (posture == Posture::AnyExceptDenied).then_some((default_mode, editable)),
        can_add: editable,
        gateway_line: match level {
            Level::Gateway => None,
            Level::Account => eff.map(|e| format!("{} {}", T::GATEWAY_PREFIX, e.gateway_summary)),
        },
        summary: match level {
            Level::Gateway => policy.summary.clone().unwrap_or_default(),
            Level::Account => eff.map(|e| e.summary.clone()).unwrap_or_default(),
        },
        locked,
    })
}

// ---- payloads (the kit's builders: a full replacement, never a patch)

/// The kit's `workspaceNewRowMode`.
pub fn new_row_mode(level: Level, posture: Posture) -> Mode {
    match (posture, level) {
        (Posture::AnyExceptDenied, _) => Mode::Deny,
        (_, Level::Gateway) => Mode::Rw,
        _ => Mode::Ro,
    }
}

fn body(level: Level, posture: Posture, default_mode: Mode, folders: &[Rule]) -> Value {
    let rows: Vec<Value> = folders
        .iter()
        .map(|r| json!({"path": r.path, "mode": r.mode.id()}))
        .collect();
    match level {
        Level::Gateway => {
            json!({"posture": posture.id(), "default_mode": default_mode.id(), "folders": rows})
        }
        Level::Account => {
            json!({"configured": true, "posture": posture.id(), "default_mode": default_mode.id(), "folders": rows})
        }
    }
}

/// Change one row's mode.
pub fn mode_payload(level: Level, p: &Policy, path: &str, mode: Mode) -> Value {
    let folders: Vec<Rule> = p
        .folders
        .iter()
        .map(|r| {
            if r.path == path {
                Rule {
                    path: path.to_string(),
                    mode,
                }
            } else {
                r.clone()
            }
        })
        .collect();
    body(level, p.posture, p.default_mode, &folders)
}

/// Change the posture (rows kept).
pub fn posture_payload(level: Level, p: &Policy, posture: Posture) -> Value {
    body(level, posture, p.default_mode, &p.folders)
}

/// Change the mode of everything else (posture b).
pub fn default_mode_payload(level: Level, p: &Policy, mode: Mode) -> Value {
    body(level, p.posture, mode, &p.folders)
}

/// Add a workspace (trimmed; the gateway checks it and answers its sentence).
pub fn add_payload(level: Level, p: &Policy, path: &str) -> Value {
    let mut folders = p.folders.clone();
    folders.push(Rule {
        path: path.trim().to_string(),
        mode: new_row_mode(level, p.posture),
    });
    body(level, p.posture, p.default_mode, &folders)
}

/// Remove a workspace.
pub fn remove_payload(level: Level, p: &Policy, path: &str) -> Value {
    let folders: Vec<Rule> = p
        .folders
        .iter()
        .filter(|r| r.path != path)
        .cloned()
        .collect();
    body(level, p.posture, p.default_mode, &folders)
}

/// The follow switch: ON = {configured:false}; OFF = what applies now (the
/// gateway's effective answer, verbatim) as this level's own rows.
pub fn follow_payload(state: &State, follow: bool) -> Result<Value, String> {
    if follow {
        return Ok(json!({"configured": false}));
    }
    let e = state
        .effective
        .as_ref()
        .ok_or("The follow switch needs the gateway's effective workspaces.")?;
    let folders: Vec<Value> = e
        .folders
        .iter()
        .map(|f| json!({"path": f.path, "mode": f.mode.id()}))
        .collect();
    Ok(json!({
        "configured": true,
        "posture": e.posture.id(),
        "default_mode": e.default_mode.unwrap_or(state.policy.default_mode).id(),
        "folders": folders,
    }))
}

/// The gateway's sentence from a refusal (the kit's `workspaceErrorSentence`):
/// `{detail: {reason, message, path}}` (unwrapped into `body`) or
/// `{detail: "<sentence>"}` (the error's message).
pub fn error_sentence(e: &ApiError) -> String {
    if let Some(m) = e
        .body
        .as_ref()
        .and_then(|b| b.get("message"))
        .and_then(Value::as_str)
    {
        return m.to_string();
    }
    match e.kind {
        ApiErrorKind::Forbidden | ApiErrorKind::Http(_) => e.message.clone(),
        _ => e.to_string(),
    }
}

/// A refusal for the row: "<gateway sentence> Not saved." (the kit's
/// `workspaceRefusal`).
pub fn refusal_text(sentence: &str) -> String {
    let s = sentence.trim();
    let s = if s.is_empty() {
        "The gateway refused the change."
    } else {
        s
    };
    let stop = if s.ends_with(['.', '!', '?']) {
        ""
    } else {
        "."
    };
    format!("{s}{stop} {}", T::NOT_SAVED)
}

// ---------------------------------------------------------------------------
// The overlay
// ---------------------------------------------------------------------------

/// Which level an overlay edits.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Target {
    /// "Eligible workspaces" (admins).
    Gateway,
    /// One account's default: `key` is `me` for the signed-in user's own
    /// row, else `tenant:id`; `id` names it in the title.
    Account { key: String, id: String },
}

impl Target {
    pub fn level(&self) -> Level {
        match self {
            Target::Gateway => Level::Gateway,
            Target::Account { .. } => Level::Account,
        }
    }
    /// The level's route under `/api/gateway` (the kit's `workspacePolicyPath`).
    pub fn path(&self) -> String {
        match self {
            Target::Gateway => "/workspace/policy".into(),
            Target::Account { key, .. } => {
                format!("/workspace/policy/{}", crate::api::urlencode(key))
            }
        }
    }
    /// The overlay's title: "Eligible workspaces" / "Workspaces — <id>".
    pub fn title(&self) -> String {
        match self {
            Target::Gateway => T::GATEWAY_TITLE.to_string(),
            Target::Account { id, .. } => format!("{} — {id}", T::TITLE),
        }
    }
    /// The load-error subject (the web's `what`).
    pub fn what(&self) -> String {
        match self {
            Target::Gateway => "The eligible workspaces".into(),
            Target::Account { id, .. } => format!("{id}'s workspaces"),
        }
    }
    /// The JSON-lane slot of the level's read.
    pub fn slot(&self) -> String {
        match self {
            Target::Gateway => "ws.gateway".into(),
            Target::Account { key, .. } => format!("ws.account.{key}"),
        }
    }
    fn write_key(&self) -> String {
        format!("{}.write", self.slot())
    }
}

/// One selectable line of the overlay.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Item {
    Follow,
    Posture,
    Row(String),
    EverythingElse,
    Add,
}

impl Item {
    /// The note key (the kit's change key).
    fn key(&self) -> String {
        match self {
            Item::Follow => "follow".into(),
            Item::Posture => "posture".into(),
            Item::Row(p) => format!("row:{p}"),
            Item::EverythingElse => "everything-else".into(),
            Item::Add => "add".into(),
        }
    }
}

/// The selectable items of a view, top to bottom.
pub fn items(v: &ChooserView) -> Vec<Item> {
    let mut out = Vec::new();
    if v.level == Level::Account && !v.locked {
        out.push(Item::Follow);
    }
    let editable = !v.following && !v.locked;
    if editable {
        out.push(Item::Posture);
        for r in v.allowed_rows().into_iter().chain(v.refused_rows()) {
            if r.editable {
                out.push(Item::Row(r.path.clone()));
            }
        }
        if v.everything_else.is_some_and(|(_, e)| e) {
            out.push(Item::EverythingElse);
        }
        if v.can_add {
            out.push(Item::Add);
        }
    }
    out
}

/// The key hints of the overlay.
pub const HINTS: &[(&str, &str)] = &[
    ("Tab", "next control"),
    ("Enter/Space", "press / pick"),
    ("Esc", "close"),
];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum NoteTone {
    Ok,
    Error,
}

#[derive(Clone, Copy)]
struct St {
    /// The add field's text.
    draft: Signal<String>,
    notes: Signal<Vec<(String, String, NoteTone)>>,
    /// The item key of the write in flight.
    pending: Signal<Option<String>>,
    /// The item last changed: its control takes the keyboard back after
    /// the re-read rebuilds the body (keyboard users keep their place).
    last: Signal<Option<String>>,
}

fn set_note(st: &St, key: &str, text: &str, tone: NoteTone) {
    st.notes.update(|n| {
        n.retain(|(k, _, _)| k != key);
        n.push((key.to_string(), text.to_string(), tone));
    });
}

/// (Re)read the level's route into its slot.
fn load(ctx: &Ctx, target: &Target) {
    ctx.store.json.set(&target.slot(), Loadable::Loading);
    ctx.send(Cmd::Json(JsonCmd::get(&target.slot(), target.path())));
}

/// Send ONE PUT of `body` for `item`, then re-read the level.
fn put(ctx: &Ctx, st: &St, target: &Target, item: &Item, body: Value) {
    let wk = target.write_key();
    st.pending.set(Some(item.key()));
    st.last.set(Some(item.key()));
    st.notes.update(|n| n.retain(|(k, _, _)| *k != item.key()));
    ctx.store.json.set_write(&wk, Some(WriteState::Pending));
    ctx.send(Cmd::Json(JsonCmd::Send {
        key: wk,
        method: "PUT".into(),
        path: target.path(),
        body,
        slow: false,
        label: target.title(),
        reload: vec![(target.slot(), target.path())],
        journal: false,
    }));
}


fn note_of(st: &St, key: &str) -> Option<(String, NoteTone)> {
    st.notes.with_untracked(|n| {
        n.iter()
            .find(|(k, _, _)| k == key)
            .map(|(_, t, tone)| (t.clone(), *tone))
    })
}

/// A segmented control as text: the current option in brackets, the
/// unavailable ones marked `×` (their reason is printed under the row).
pub fn segmented(options: &[(String, bool)], current: usize) -> String {
    options
        .iter()
        .enumerate()
        .map(|(i, (label, available))| {
            if i == current {
                format!("[{label}]")
            } else if !available {
                format!(" {label}× ")
            } else {
                format!(" {label} ")
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}




/// Open the chooser for `target` (R15: ONE form modal — every control a
/// widget: the follow Toggle, the posture Segmented, each row's permission
/// Segmented with above-cap modes disabled + the kit sentence, a remove
/// button, the add field + Add; one PUT per change, "Saved" / the
/// gateway's sentence + "Not saved." under the control). Gateway level:
/// admins only.
pub fn open(cx: Scope, ctx: &Ctx, target: Target) {
    if target == Target::Gateway
        && !super::util::admin_gate(&ctx.store, "changing the eligible workspaces")
    {
        return;
    }
    if !ctx
        .store
        .conn
        .with_untracked(crate::store::ConnPhase::is_connected)
    {
        ctx.store.notice.set(Some(
            "not connected — probe on the Connection screen first".into(),
        ));
        return;
    }
    load(ctx, &target);
    let c = ctx.clone();
    let level = target.level();
    super::w::FormModal::new(target.title())
        .lead(level.help())
        .size(104, 36)
        .open(ctx, cx, move |mcx, close, _guard, w| {
            let st = St {
                draft: mcx.signal(String::new()),
                notes: mcx.signal(Vec::new()),
                pending: mcx.signal(None),
                last: mcx.signal(None),
            };
            // The write's outcome beside its item ("Saved" / the refusal).
            {
                let store = c.store;
                let wk = target.write_key();
                mcx.effect(move || {
                    let w = store.json.write(&wk);
                    let Some(key) = st.pending.get_untracked() else {
                        return;
                    };
                    match w {
                        Some(WriteState::Done(_)) => {
                            set_note(&st, &key, T::SAVED, NoteTone::Ok);
                            if key == "add" {
                                st.draft.set(String::new());
                            }
                        }
                        Some(WriteState::Failed(e)) => set_note(
                            &st,
                            &key,
                            &refusal_text(&error_sentence(&e)),
                            NoteTone::Error,
                        ),
                        _ => return,
                    }
                    st.pending.set(None);
                    store.json.set_write(&wk, None);
                });
            }
            let body_ctx = c.clone();
            let tgt = target.clone();
            let t = use_theme(mcx).get().tokens;
            let close2 = close.clone();
            let body = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
                chooser_body(gcx, &body_ctx, &tgt, st, w)
            });
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    Scroll::new(body)
                        .axes(false, true)
                        .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                        .scrollbar_auto_hide(true)
                        .view(mcx),
                )
                .child(super::w::form::button_row(vec![super::w::action::button(
                    mcx,
                    &t,
                    &super::w::Action::label("close", "Close"),
                    super::w::action::On::Raised,
                    true,
                    move || close2(),
                )]))
                .build()
        });
}

/// One write at a time (a press while one is in flight would act on the
/// state shown before it landed): Some(()) when free.
fn free(st: &St) -> bool {
    st.pending.with_untracked(Option::is_none)
}

fn note_line(st: St, key: String, width: i32) -> View {
    dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
        let t = abstracttui::app::current_theme().tokens;
        let _ = st.notes.get();
        match note_of(&st, &key) {
            Some((text, tone)) => super::w::form::sentence(
                &t,
                &text,
                width,
                if tone == NoteTone::Ok { t.ok } else { t.error },
            ),
            None => Element::new().style(LayoutStyle::default().h(0)).build(),
        }
    })
}

fn indent(v: View, n: i32) -> View {
    Element::new()
        .style(LayoutStyle::column().shrink(0.0).padding(Edges {
            left: n,
            right: 0,
            top: 0,
            bottom: 0,
        }))
        .child(v)
        .build()
}

fn chooser_body(cx: Scope, ctx: &Ctx, target: &Target, st: St, width: i32) -> View {
    use super::w::paint::{fill_line, Ink};
    use super::w::form::sentence;
    let t = use_theme(cx).get().tokens;
    let level = target.level();
    let slot = ctx.store.json.get(&target.slot());
    let state = match slot {
        Loadable::Ready(v) => as_state(&v, level),
        Loadable::Failed(e) => Err(error_sentence(&e)),
        _ => return sentence(&t, T::LOADING, width, t.text_muted),
    };
    let view = state
        .as_ref()
        .map_err(Clone::clone)
        .and_then(|s| chooser_view(level, s));
    let (state, view) = match (state, view) {
        (Ok(s), Ok(v)) => (s, v),
        (Err(e), _) | (_, Err(e)) => {
            return sentence(
                &t,
                &format!("{} could not be loaded: {e}", target.what()),
                width,
                t.error,
            )
        }
    };
    let busy_key = st.pending.get();
    let last = st.last.get_untracked();
    let is_last = |k: &str| last.as_deref() == Some(k);
    let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
    let heading = |s: &str| {
        fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new(s, t.text).bold()],
            None,
        )
    };
    if let Some(g) = &view.gateway_line {
        col = col.child(sentence(&t, g, width, t.text));
    }
    if view.locked {
        col = col.child(sentence(&t, T::LOCKED, width, t.warn));
    }
    // ---- Follow the gateway policy (account level)
    if level == Level::Account {
        let it = Item::Follow;
        let c = ctx.clone();
        let tgt = target.clone();
        let s2 = state.clone();
        let tg = super::w::Toggle::new(view.following)
            .label(T::FOLLOW_GATEWAY)
            .busy(busy_key.as_deref() == Some("follow"))
            .refused(view.locked.then(|| T::LOCKED.to_string()))
            .tip(T::FOLLOW_GATEWAY_HELP)
            .autofocus(is_last("follow"))
            .on_change(move |want| {
                if !free(&st) {
                    return;
                }
                match follow_payload(&s2, want) {
                    Ok(b) => put(&c, &st, &tgt, &Item::Follow, b),
                    Err(e) => set_note(&st, "follow", &e, NoteTone::Error),
                }
            });
        col = col
            .child(
                Element::new()
                    .style(LayoutStyle::line(1).shrink(0.0))
                    .child(tg.view(cx, &t))
                    .build(),
            )
            .child(indent(sentence(&t, T::FOLLOW_GATEWAY_HELP, width - 3, t.text_muted), 3))
            .child(note_line(st, it.key(), width));
    }
    // ---- The posture
    col = col.child(heading(T::POSTURE_LABEL));
    if view.following || view.locked {
        col = col.child(indent(sentence(&t, view.posture.label(), width - 2, t.text), 2));
    } else {
        let cur = POSTURES.iter().position(|p| *p == view.posture);
        let labels: Vec<String> = POSTURES.iter().map(|p| p.label().to_string()).collect();
        let narrow = labels.iter().map(|l| abstracttui::text::width(l) + 3).sum::<i32>() > width - 2;
        let c = ctx.clone();
        let tgt = target.clone();
        let pol = state.policy.clone();
        let seg = super::w::Segmented::new(labels, cur)
            .vertical(narrow)
            .autofocus_chosen(is_last("posture"))
            .on_pick(move |i| {
                if free(&st) && Some(i) != cur {
                    put(&c, &st, &tgt, &Item::Posture, posture_payload(level, &pol, POSTURES[i]));
                }
            });
        col = col.child(indent(seg.view(cx, &t), 2));
    }
    col = col
        .child(indent(sentence(&t, view.posture.help(), width - 2, t.text_muted), 2))
        .child(note_line(st, Item::Posture.key(), width));
    // ---- Rows: Allowed, then Refused (built-in refusals last, fixed)
    let editable_level = !view.following && !view.locked;
    let rows_block = |col: Element, title: &str, rows: &[&ViewRow]| -> Element {
        if rows.is_empty() {
            return col;
        }
        let mut col = col.child(heading(title));
        for r in rows {
            let it = Item::Row(r.path.clone());
            // The path, wrapped, with its remove button (editable rows).
            let mut head = Element::new().style(LayoutStyle::row().shrink(0.0));
            head = head.child(indent(
                sentence(
                    &t,
                    &r.path,
                    width - 8,
                    if r.builtin { t.text_muted } else { t.text },
                ),
                2,
            ));
            if r.editable && editable_level && !r.builtin {
                let c = ctx.clone();
                let tgt = target.clone();
                let pol = state.policy.clone();
                let path = r.path.clone();
                let a = super::w::Action::glyph("archive", T::REMOVE)
                    .tooltip(format!("{} {}", T::REMOVE, r.path))
                    .danger();
                head = head
                    .child(Element::new().style(LayoutStyle::default().grow(1.0)).build())
                    .child(super::w::action::button(
                        cx,
                        &t,
                        &a,
                        super::w::action::On::Raised,
                        true,
                        move || {
                            if free(&st) {
                                put(
                                    &c,
                                    &st,
                                    &tgt,
                                    &Item::Row(path.clone()),
                                    remove_payload(level, &pol, &path),
                                );
                            }
                        },
                    ));
            }
            col = col.child(head.build());
            if r.builtin {
                col = col.child(indent(
                    sentence(
                        &t,
                        &format!("{} — {}", Mode::Deny.label(), T::BUILTIN_REFUSED),
                        width - 4,
                        t.text_muted,
                    ),
                    4,
                ));
            } else if !r.editable || !editable_level {
                col = col.child(indent(sentence(&t, r.mode.label(), width - 4, t.text_muted), 4));
            } else {
                let cur = MODES.iter().position(|m| *m == r.mode);
                let mut seg =
                    super::w::Segmented::new(MODES.iter().map(|m| m.label().to_string()), cur);
                for (i, m) in MODES.iter().enumerate() {
                    if let Some(why) = r.reason(*m) {
                        seg = seg.disable(i, why);
                    }
                }
                let c = ctx.clone();
                let tgt = target.clone();
                let pol = state.policy.clone();
                let path = r.path.clone();
                let reasons = r.reasons.clone();
                let seg = seg.autofocus_chosen(is_last(&it.key())).on_pick(move |i| {
                    let want = MODES[i];
                    if !free(&st) || Some(i) == cur {
                        return;
                    }
                    // Never sent above the cap (the button is disabled too).
                    if reasons.iter().any(|(m, _)| *m == want) {
                        return;
                    }
                    put(&c, &st, &tgt, &Item::Row(path.clone()), mode_payload(level, &pol, &path, want));
                });
                col = col.child(
                    Element::new()
                        .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                        .child(indent(
                            fill_line(
                                LayoutStyle::default().width(Dimension::Cells(12)).height(Dimension::Cells(1)),
                                vec![Ink::new(format!("{}:", T::ACCESS_LABEL), t.text_muted)],
                                None,
                            ),
                            4,
                        ))
                        .child(seg.view(cx, &t))
                        .build(),
                );
                for (md, why) in &r.reasons {
                    col = col.child(indent(
                        sentence(&t, &format!("{}: {why}", md.label()), width - 4, t.text_faint),
                        4,
                    ));
                }
            }
            col = col.child(note_line(st, it.key(), width));
        }
        col
    };
    let allowed = view.allowed_rows();
    let refused = view.refused_rows();
    col = rows_block(col, T::ALLOWED_TITLE, &allowed);
    col = rows_block(col, T::DENIED_TITLE, &refused);
    // ---- Everything else (posture b)
    if let Some((mode, editable)) = view.everything_else {
        col = col.child(heading(T::EVERYTHING_ELSE));
        if editable && editable_level {
            let cur = ACCESS.iter().position(|a| *a == mode);
            let c = ctx.clone();
            let tgt = target.clone();
            let pol = state.policy.clone();
            let seg = super::w::Segmented::new(ACCESS.iter().map(|m| m.label().to_string()), cur)
                .autofocus_chosen(is_last("everything-else"))
                .on_pick(move |i| {
                    if free(&st) && Some(i) != cur {
                        put(
                            &c,
                            &st,
                            &tgt,
                            &Item::EverythingElse,
                            default_mode_payload(level, &pol, ACCESS[i]),
                        );
                    }
                });
            col = col.child(indent(seg.view(cx, &t), 4));
        } else {
            col = col.child(indent(sentence(&t, mode.label(), width - 4, t.text_muted), 4));
        }
        col = col.child(note_line(st, Item::EverythingElse.key(), width));
    }
    // ---- The add row: a text field + Add (Enter in the field adds too).
    if view.can_add && editable_level {
        let c = ctx.clone();
        let tgt = target.clone();
        let pol = state.policy.clone();
        let add = std::rc::Rc::new(move || {
            let typed = st.draft.get_untracked();
            if typed.trim().is_empty() || !free(&st) {
                return;
            }
            put(&c, &st, &tgt, &Item::Add, add_payload(level, &pol, &typed));
        });
        let add_enter = add.clone();
        let field = TextInput::new()
            .value(st.draft)
            .placeholder(T::ADD_PLACEHOLDER)
            .on_submit(move |_| add_enter())
            .layout(LayoutStyle::default().grow(1.0).h(1))
            .element(cx, &t);
        let field = if is_last("add") { field.autofocus() } else { field }.build();
        let a = super::w::Action::label("add", T::ADD);
        let btn = super::w::action::button(cx, &t, &a, super::w::action::On::Raised, true, move || add());
        col = col.child(
            Element::new()
                .style(LayoutStyle::row().height(Dimension::Cells(1)).gap(1).shrink(0.0))
                .child(field)
                .child(btn)
                .build(),
        );
        col = col.child(note_line(st, Item::Add.key(), width));
    }
    if view.posture == Posture::AllowedOnly && allowed.is_empty() {
        col = col.child(sentence(&t, T::EMPTY_ALLOWED, width, t.text_muted));
    }
    // The effective line, verbatim.
    col = col
        .child(Element::new().style(LayoutStyle::line(1).shrink(0.0)).build())
        .child(sentence(&t, &view.summary, width, t.text));
    col.build()
}

// ---------------------------------------------------------------------------
// The command sandbox state line (R12.1; the web's Accounts head)
// ---------------------------------------------------------------------------

/// (state line, its sentence, warn?) for the host's command sandbox, from
/// the `GET /workspace/policy` read — the web console's Accounts line,
/// verbatim (`command_sandbox.line`; the sentence is the web's tooltip).
pub fn sandbox_line(slot: &Loadable<Value>) -> (String, String, bool) {
    match slot {
        Loadable::Ready(v) => {
            let cs = v.get("command_sandbox");
            match cs.and_then(|c| c.get("line")).and_then(Value::as_str) {
                Some(l) => {
                    let sentence = cs
                        .and_then(|c| c.get("sentence"))
                        .and_then(Value::as_str)
                        .unwrap_or(l);
                    let state = cs
                        .and_then(|c| c.get("state"))
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    (l.to_string(), sentence.to_string(), state != "sandboxed")
                }
                None => (
                    "Commands: GET /workspace/policy answered without command_sandbox (round 12 seam)."
                        .into(),
                    "The command sandbox state could not be read.".into(),
                    true,
                ),
            }
        }
        Loadable::Failed(e) => (
            format!("Commands: {}", error_sentence(e)),
            "The command sandbox state could not be read.".into(),
            true,
        ),
        // Hidden until the read answers (the web's line starts hidden).
        Loadable::Loading | Loadable::NotAsked => (String::new(), String::new(), false),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fx(name: &str) -> Value {
        let path = format!(
            "{}/tests/fixtures/r14w3_{name}.json",
            env!("CARGO_MANIFEST_DIR")
        );
        serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
    }

    /// The Rust table IS the kit's table: same keys, same order, same bytes.
    #[test]
    fn the_table_is_the_kits_byte_for_byte() {
        let kit = fx("kit_workspace_chooser_text");
        let kit: Vec<(String, String)> = kit
            .as_array()
            .expect("[[key, value], …]")
            .iter()
            .map(|p| {
                (
                    p[0].as_str().unwrap().to_string(),
                    p[1].as_str().unwrap().to_string(),
                )
            })
            .collect();
        let ours: Vec<(String, String)> = TABLE
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        assert_eq!(
            ours, kit,
            "src/ui/workspace_chooser.rs TABLE differs from the kit's WORKSPACE_CHOOSER_TEXT"
        );
    }

    /// The live kit file (abstractuic checkout beside the gateway, or
    /// $ABSTRACTUIC_DIR): ignored by default because the published crate
    /// has no kit; when run, a missing kit FAILS.
    #[test]
    #[ignore]
    fn the_table_matches_the_live_kit_file() {
        let base = std::env::var("ABSTRACTUIC_DIR")
            .unwrap_or_else(|_| format!("{}/../../abstractuic", env!("CARGO_MANIFEST_DIR")));
        let path = format!("{base}/ui-kit/src/workspace_chooser_core.ts");
        let src = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("the kit table is not readable at {path}: {e}"));
        let start = src
            .find("export const WORKSPACE_CHOOSER_TEXT = {\n")
            .expect("the kit's WORKSPACE_CHOOSER_TEXT block");
        let block = &src[start..];
        let end = block.find("\n} as const;").expect("the block's end");
        let mut kit = Vec::new();
        for l in block[..end].lines().skip(1) {
            let (k, v) = l.trim().split_once(": ").expect("key: \"value\",");
            let v: String = serde_json::from_str(v.trim_end_matches(',')).expect("a JSON string");
            kit.push((k.to_string(), v));
        }
        let ours: Vec<(String, String)> = TABLE
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        assert_eq!(ours, kit);
    }

    #[test]
    fn a_gateway_answer_parses_and_lists_builtins_fixed() {
        let s = as_state(&fx("policy_allowed"), Level::Gateway).unwrap();
        let v = chooser_view(Level::Gateway, &s).unwrap();
        assert_eq!(v.posture, Posture::AllowedOnly);
        assert!(v.gateway_line.is_none());
        assert_eq!(
            v.summary,
            "Deny everything, allow listed workspaces · /srv/w3/ws/proj (rw) · /srv/w3/ws/archive (ro)"
        );
        assert_eq!(v.allowed_rows().len(), 2);
        assert!(v.refused_rows().iter().all(|r| r.builtin && !r.editable));
        assert_eq!(v.refused_rows().len(), 12);
        // Gateway rows offer every mode (the cap is what the admin sets).
        assert!(v
            .rows
            .iter()
            .filter(|r| !r.builtin)
            .all(|r| r.allowed.len() == 3));
        assert!(v.everything_else.is_none());
        assert_eq!(
            items(&v),
            vec![
                Item::Posture,
                Item::Row("/srv/w3/ws/proj".into()),
                Item::Row("/srv/w3/ws/archive".into()),
                Item::Add
            ]
        );
    }

    #[test]
    fn an_account_answer_caps_modes_with_the_kit_sentences() {
        let s = as_state(&fx("account_alice_configured"), Level::Account).unwrap();
        let v = chooser_view(Level::Account, &s).unwrap();
        assert!(!v.following);
        assert_eq!(
            v.gateway_line.as_deref(),
            Some("Gateway: Deny everything, allow listed workspaces · /srv/w3/ws/proj (rw) · /srv/w3/ws/archive (ro)")
        );
        let archive = v
            .rows
            .iter()
            .find(|r| r.path.ends_with("/archive"))
            .unwrap();
        assert_eq!(archive.cap, Some(Mode::Ro));
        assert_eq!(archive.allowed, vec![Mode::Ro, Mode::Deny]);
        assert_eq!(
            archive.reason(Mode::Rw),
            Some("The gateway allows this workspace read-only")
        );
        assert_eq!(items(&v)[0], Item::Follow);
        // Under "Allow everything…" a gateway-refused path is capped at Refused.
        let s = as_state(&fx("account_alice_any"), Level::Account).unwrap();
        let v = chooser_view(Level::Account, &s).unwrap();
        assert_eq!(v.everything_else, Some((Mode::Ro, true)));
        assert_eq!(modes_under_cap(Some(Mode::Deny)).0, vec![Mode::Deny]);
        assert_eq!(
            modes_under_cap(Some(Mode::Deny)).1,
            vec![
                (Mode::Rw, "The gateway refuses this workspace"),
                (Mode::Ro, "The gateway refuses this workspace")
            ]
        );
    }

    #[test]
    fn following_shows_what_applies_read_only() {
        let s = as_state(&fx("account_alice_follow"), Level::Account).unwrap();
        let v = chooser_view(Level::Account, &s).unwrap();
        assert!(v.following);
        assert!(!v.can_add);
        assert!(v.rows.iter().all(|r| !r.editable));
        assert_eq!(items(&v), vec![Item::Follow]);
        // OFF starts from the effective answer, verbatim.
        let b = follow_payload(&s, false).unwrap();
        assert_eq!(b["configured"], true);
        assert_eq!(b["posture"], "allowed_only");
        assert_eq!(b["folders"].as_array().unwrap().len(), 2);
        assert_eq!(
            follow_payload(&s, true).unwrap(),
            json!({"configured": false})
        );
    }

    #[test]
    fn an_older_or_partial_answer_fails_loudly() {
        let mut v = fx("policy_allowed");
        v["policy"]["shared_workspace"] = json!("/x");
        assert_eq!(as_state(&v, Level::Gateway).unwrap_err(), OLD_MODEL);
        let mut v = fx("policy_allowed");
        v["policy"].as_object_mut().unwrap().remove("summary");
        assert!(as_state(&v, Level::Gateway)
            .unwrap_err()
            .starts_with("The gateway answered without a workspace policy"));
        let mut v = fx("account_alice_configured");
        v["effective"]["folders"][0]
            .as_object_mut()
            .unwrap()
            .remove("cap");
        assert!(as_state(&v, Level::Account)
            .unwrap_err()
            .starts_with("The gateway answered without the effective workspaces"));
    }

    #[test]
    fn payloads_are_full_replacements() {
        let s = as_state(&fx("policy_allowed"), Level::Gateway).unwrap();
        let p = &s.policy;
        let b = mode_payload(Level::Gateway, p, "/srv/w3/ws/proj", Mode::Ro);
        assert_eq!(
            b,
            json!({"posture": "allowed_only", "default_mode": "rw", "folders": [
                {"path": "/srv/w3/ws/proj", "mode": "ro"}, {"path": "/srv/w3/ws/archive", "mode": "ro"}]})
        );
        let b = add_payload(Level::Gateway, p, "  /new  ");
        assert_eq!(b["folders"][2], json!({"path": "/new", "mode": "rw"}));
        let b = remove_payload(Level::Gateway, p, "/srv/w3/ws/proj");
        assert_eq!(b["folders"].as_array().unwrap().len(), 1);
        let b = posture_payload(Level::Gateway, p, Posture::AnyExceptDenied);
        assert_eq!(b["posture"], "any_except_denied");
        assert!(b.get("configured").is_none());
        let s = as_state(&fx("account_alice_configured"), Level::Account).unwrap();
        let b = add_payload(Level::Account, &s.policy, "/x");
        assert_eq!(b["configured"], true);
        assert_eq!(b["folders"][2]["mode"], "ro");
        let s = as_state(&fx("account_alice_any"), Level::Account).unwrap();
        assert_eq!(
            add_payload(Level::Account, &s.policy, "/x")["folders"][2]["mode"],
            "deny"
        );
        assert_eq!(
            default_mode_payload(Level::Account, &s.policy, Mode::Rw)["default_mode"],
            "rw"
        );
    }

    #[test]
    fn a_refusal_is_the_gateways_sentence_then_not_saved() {
        let body = fx("account_alice_refused_cap");
        let e = ApiError {
            kind: ApiErrorKind::Http(400),
            message: body["detail"].to_string(),
            body: Some(body["detail"].clone()),
            timed_out: false,
        };
        assert_eq!(
            refusal_text(&error_sentence(&e)),
            "The gateway allows this workspace read-only: /srv/w3/ws/archive. Not saved."
        );
        let e = ApiError {
            kind: ApiErrorKind::Forbidden,
            message: "Only an admin or castor's creator can change its workspaces.".into(),
            body: Some(
                json!({"detail": "Only an admin or castor's creator can change its workspaces."}),
            ),
            timed_out: false,
        };
        assert_eq!(
            refusal_text(&error_sentence(&e)),
            "Only an admin or castor's creator can change its workspaces. Not saved."
        );
        assert_eq!(
            refusal_text(""),
            "The gateway refused the change. Not saved."
        );
        assert_eq!(refusal_text("No"), "No. Not saved.");
    }

    #[test]
    fn targets_name_their_routes_and_titles() {
        assert_eq!(Target::Gateway.path(), "/workspace/policy");
        assert_eq!(Target::Gateway.title(), "Eligible workspaces");
        let a = Target::Account {
            key: "default:castor".into(),
            id: "castor".into(),
        };
        assert_eq!(a.path(), "/workspace/policy/default%3Acastor");
        assert_eq!(a.title(), "Workspaces — castor");
        assert_eq!(a.what(), "castor's workspaces");
    }

    #[test]
    fn the_sandbox_line_is_the_gateways_verbatim() {
        let (l, s, warn) = sandbox_line(&Loadable::Ready(fx("policy_fresh")));
        assert_eq!(l, "Commands sandboxed: macOS sandbox-exec");
        assert_eq!(
            s,
            "Every command a run starts is confined by the operating system to that run's workspaces."
        );
        assert!(!warn);
        let refused = Loadable::Ready(json!({"command_sandbox": {"state": "refused",
            "line": "Commands refused: no sandbox on this host", "sentence": "x"}}));
        let (l, _, warn) = sandbox_line(&refused);
        assert_eq!(l, "Commands refused: no sandbox on this host");
        assert!(warn);
        assert_eq!(sandbox_line(&Loadable::Loading).0, "");
        let (l, _, warn) = sandbox_line(&Loadable::Ready(json!({"policy": {}})));
        assert!(l.contains("answered without command_sandbox"));
        assert!(warn);
    }

    #[test]
    fn segmented_marks_current_and_unavailable() {
        let s = segmented(
            &[
                ("Read & write".into(), false),
                ("Read-only".into(), true),
                ("Refused".into(), true),
            ],
            1,
        );
        assert_eq!(s, " Read & write×  [Read-only]  Refused ");
    }
}
