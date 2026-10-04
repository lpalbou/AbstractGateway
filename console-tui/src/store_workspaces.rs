//! The Workspaces page (R8.2, round 8): which folders agents may read and
//! write — the gateway-wide policy and the per-account policies, the web
//! console's "Workspaces" sidebar entry in the terminal. Pure data + the
//! page's sentences (unit-tested here); the screen is `ui/workspaces.rs`,
//! the writes `worker_workspaces.rs`.
//!
//! Routes (the web's own): the gateway policy = `GET/POST
//! /admin/runtime-config` (`workspace_default_mode`,
//! `trust_client_launch_folder`, `workspace_allowed_paths`,
//! `workspace_blocked_paths`); one account = `PUT
//! /admin/user-workspace-policy?tenant_id&user_id` (`{policy: entry}`,
//! `{policy: null}` = follow the gateway policy) with the stored entries in
//! the config's `user_workspace_policies`; a non-admin's own policy =
//! `GET/PUT /workspace/policy/self`; a typed folder = `POST
//! /workspace/path-check` first (refused → not saved, its sentence shown).

use abstracttui::prelude::*;
use serde_json::{json, Map, Value};

use super::skills::Tone;
use super::RuntimeConfigData;

/// The access mode's two segments (the web's segmented switch).
pub const MODE_ALLOW_LIST: &str = "Allow my list";
pub const MODE_ALLOW_ALL: &str = "Allow everything except";
pub const MODE_HELP_LIST: &str =
    "Agents may use only the allowed folders below (and the launch folder while it is trusted).";
pub const MODE_HELP_ALL: &str = "Agents may use every folder except the refused ones below.";
pub const TRUST_LABEL: &str = "Trust the launch folder";
pub const TRUST_HELP: &str = "An agent may read and write the folder its app was started from.";
pub const ALLOWED_TITLE: &str = "Allowed folders";
pub const REFUSED_TITLE: &str = "Refused folders";
pub const REFUSED_HELP: &str = "Refused in every mode.";
pub const ADD_FOLDER: &str = "+ Add a folder";
pub const FOLLOW_GATEWAY: &str = "Follow the gateway policy";
pub const TITLE: &str = "Workspaces";
pub const SUBTITLE: &str = "Which folders agents may read and write";

/// The mode word for a stored/served mode ("blacklist" = allow all).
pub fn mode_label(mode: &str) -> &'static str {
    if mode == "blacklist" {
        MODE_ALLOW_ALL
    } else {
        MODE_ALLOW_LIST
    }
}

fn strs(v: &Value, k: &str) -> Vec<String> {
    match v.get(k) {
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(Value::as_str)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
        Some(Value::String(s)) => s
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .collect(),
        _ => Vec::new(),
    }
}

fn lines(raw: &str) -> Vec<String> {
    raw.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect()
}

/// Which list a folder row belongs to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ListKind {
    Allowed,
    Refused,
}

impl ListKind {
    pub fn title(self) -> &'static str {
        match self {
            ListKind::Allowed => ALLOWED_TITLE,
            ListKind::Refused => REFUSED_TITLE,
        }
    }
    /// The gateway-wide config key.
    pub fn gateway_key(self) -> &'static str {
        match self {
            ListKind::Allowed => "workspace_allowed_paths",
            ListKind::Refused => "workspace_blocked_paths",
        }
    }
}

/// The policy a scope shows: the stored choices (None = follows the
/// gateway) over the gateway's values.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Policy {
    /// "whitelist" | "blacklist"; None = follows the gateway (accounts).
    pub mode: Option<String>,
    /// None = follows the gateway (accounts).
    pub trust: Option<bool>,
    pub allowed: Vec<String>,
    pub refused: Vec<String>,
    /// Fields the editor never shows but a rewrite must keep (the
    /// admin-classed scope-override grant on an account entry).
    pub keep: Map<String, Value>,
}

impl Policy {
    /// A stored per-account entry (`user_workspace_policies["t:u"]` or the
    /// self route's `policy`).
    pub fn from_entry(v: &Value) -> Policy {
        let mut keep = Map::new();
        if let Some(x) = v.get("client_workspace_scope_overrides") {
            if !x.is_null() {
                keep.insert("client_workspace_scope_overrides".into(), x.clone());
            }
        }
        Policy {
            mode: v
                .get("mode")
                .and_then(Value::as_str)
                .filter(|m| !m.is_empty())
                .map(str::to_string),
            trust: v.get("trust_client_launch_folder").and_then(Value::as_bool),
            allowed: strs(v, "workspace_allowed_paths"),
            refused: strs(v, "workspace_blocked_paths"),
            keep,
        }
    }

    /// The gateway-wide policy as the config serves it.
    pub fn gateway(c: &RuntimeConfigData) -> Policy {
        Policy {
            mode: Some(if c.workspace_default_mode == "blacklist" {
                "blacklist".into()
            } else {
                "whitelist".into()
            }),
            trust: Some(c.trust_client_launch_folder),
            allowed: lines(&c.workspace_allowed_paths),
            refused: lines(&c.workspace_blocked_paths),
            keep: Map::new(),
        }
    }

    pub fn list(&self, kind: ListKind) -> &Vec<String> {
        match kind {
            ListKind::Allowed => &self.allowed,
            ListKind::Refused => &self.refused,
        }
    }

    pub fn list_mut(&mut self, kind: ListKind) -> &mut Vec<String> {
        match kind {
            ListKind::Allowed => &mut self.allowed,
            ListKind::Refused => &mut self.refused,
        }
    }

    /// The full entry a per-account PUT sends (`{policy: <this>}`): only
    /// what is set, plus the kept fields. Empty = follows the gateway.
    pub fn entry(&self) -> Value {
        let mut m = self.keep.clone();
        if let Some(mode) = &self.mode {
            m.insert("mode".into(), json!(mode));
        }
        if let Some(t) = self.trust {
            m.insert("trust_client_launch_folder".into(), json!(t));
        }
        if !self.allowed.is_empty() {
            m.insert("workspace_allowed_paths".into(), json!(self.allowed));
        }
        if !self.refused.is_empty() {
            m.insert("workspace_blocked_paths".into(), json!(self.refused));
        }
        Value::Object(m)
    }

    /// Is anything stored (an account with its own policy)?
    pub fn is_custom(&self) -> bool {
        self.entry().as_object().is_some_and(|m| !m.is_empty())
    }

    /// The effective mode / trust over `gateway`.
    pub fn effective_mode<'a>(&'a self, gateway: &'a Policy) -> &'a str {
        self.mode
            .as_deref()
            .or(gateway.mode.as_deref())
            .unwrap_or("whitelist")
    }
    pub fn effective_trust(&self, gateway: &Policy) -> bool {
        self.trust.or(gateway.trust).unwrap_or(true)
    }
}

fn plural(n: usize, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}

/// The one-line effective summary at the top of the page (and in each
/// scope's row): mode · trust · folders.
pub fn summary(p: &Policy, gateway: &Policy) -> String {
    let mode = p.effective_mode(gateway);
    let mut parts = vec![mode_label(mode).to_string()];
    if mode == "blacklist" {
        parts.push(plural(
            p.refused.len() + gateway_refused_extra(p, gateway),
            "refused folder",
            "refused folders",
        ));
    } else {
        parts.push(plural(p.allowed.len(), "allowed folder", "allowed folders"));
        parts.push(plural(
            p.refused.len() + gateway_refused_extra(p, gateway),
            "refused folder",
            "refused folders",
        ));
    }
    parts.push(if p.effective_trust(gateway) {
        "launch folder trusted".into()
    } else {
        "launch folder not trusted".into()
    });
    parts.join(" · ")
}

/// The gateway's refused folders still apply under an account's own
/// policy (the gateway-wide deny list always applies).
fn gateway_refused_extra(p: &Policy, gateway: &Policy) -> usize {
    if std::ptr::eq(p, gateway) {
        return 0;
    }
    gateway
        .refused
        .iter()
        .filter(|g| !p.refused.contains(g))
        .count()
}

/// An account row's policy cell.
pub fn account_cell(entry: Option<&Policy>, gateway: &Policy) -> String {
    match entry.filter(|e| e.is_custom()) {
        Some(e) => format!("Own policy · {}", summary(e, gateway)),
        None => "Follows the gateway policy".into(),
    }
}

/// Which scope the editor shows.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Scope {
    Gateway,
    Account {
        tenant_id: String,
        user_id: String,
    },
    /// A non-admin's own policy (`/workspace/policy/self`).
    Own,
}

impl Scope {
    pub fn title(&self) -> String {
        match self {
            Scope::Gateway => "Gateway policy".into(),
            Scope::Account { tenant_id, user_id }
                if tenant_id == "default" || tenant_id.is_empty() =>
            {
                format!("{user_id}'s policy")
            }
            Scope::Account { tenant_id, user_id } => format!("{tenant_id}/{user_id}'s policy"),
            Scope::Own => "Your policy".into(),
        }
    }
}

/// The per-account entries of the config (`"tenant:user" -> entry`).
pub fn account_entries(c: &RuntimeConfigData) -> Map<String, Value> {
    serde_json::from_str::<Value>(&c.user_workspace_policies)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

pub fn account_policy(c: &RuntimeConfigData, tenant_id: &str, user_id: &str) -> Option<Policy> {
    account_entries(c)
        .get(&format!("{tenant_id}:{user_id}"))
        .map(Policy::from_entry)
}

/// `POST /workspace/path-check` → `{path, normalized, absolute, exists,
/// is_dir, valid, sentence}`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PathCheck {
    pub normalized: String,
    pub exists: bool,
    pub is_dir: bool,
    pub valid: bool,
    pub sentence: String,
}

impl PathCheck {
    pub fn from_value(v: &Value, typed: &str) -> PathCheck {
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let b = |k: &str| v.get(k).and_then(Value::as_bool).unwrap_or(false);
        let normalized = s("normalized");
        PathCheck {
            normalized: if normalized.is_empty() {
                typed.trim().to_string()
            } else {
                normalized
            },
            exists: b("exists"),
            is_dir: b("is_dir"),
            valid: b("valid"),
            sentence: s("sentence"),
        }
    }
}

/// What the editor's in-place input edits.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Edit {
    /// A new folder at the end of `kind`.
    Add(ListKind),
    /// Folder `index` of `kind`.
    Row(ListKind, usize),
}

/// The page's signals (ride `Store::ws`).
#[derive(Clone, Copy)]
pub struct WorkspacesStore {
    /// The scopes table's selection (0 = the gateway policy).
    pub sel: Signal<usize>,
    /// The editor overlay's selected item.
    pub item: Signal<usize>,
    /// The in-place input, when open.
    pub editing: Signal<Option<Edit>>,
    pub draft: Signal<String>,
    /// The editor's outcome line ("Saved", the path-check sentence, …).
    pub msg: Signal<Option<(String, Tone)>>,
    /// A write in flight (the editor holds new writes until it lands).
    pub busy: Signal<bool>,
    /// The Accounts Workspace jump: focus this account's row.
    pub focus: Signal<Option<(String, String)>>,
    /// A non-admin's view of the gateway policy (`GET /workspace/policy`).
    pub public: Signal<super::Loadable<Value>>,
}

impl WorkspacesStore {
    pub fn create(cx: Scope_) -> WorkspacesStore {
        WorkspacesStore {
            sel: cx.signal(0),
            item: cx.signal(0),
            editing: cx.signal(None),
            draft: cx.signal(String::new()),
            msg: cx.signal(None),
            busy: cx.signal(false),
            focus: cx.signal(None),
            public: cx.signal(super::Loadable::NotAsked),
        }
    }

    /// Forget the previous gateway's state (a reconnect).
    pub fn reset(&self) {
        self.editing.set(None);
        self.msg.set(None);
        self.busy.set(false);
        self.focus.set(None);
        self.public.set(super::Loadable::NotAsked);
    }
}

/// The reactive scope type (the page's `Scope` enum shadows the name).
pub type Scope_ = abstracttui::prelude::Scope;

/// A non-admin's read-only gateway line from `GET /workspace/policy`.
pub fn public_line(v: &Value) -> String {
    let p = v.get("policy").unwrap_or(v);
    let n = |k: &str| p.get(k).and_then(Value::as_u64).unwrap_or(0) as usize;
    let trust = p
        .get("trust_client_launch_folder")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    format!(
        "Gateway policy (set by an admin): {} · {} · {}",
        plural(
            n("extra_allowed_workspaces"),
            "allowed folder",
            "allowed folders"
        ),
        plural(
            n("blocked_workspace_roots"),
            "refused folder",
            "refused folders"
        ),
        if trust {
            "launch folder trusted"
        } else {
            "launch folder not trusted"
        }
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gw() -> Policy {
        Policy {
            mode: Some("whitelist".into()),
            trust: Some(true),
            allowed: vec!["/srv/a".into(), "/srv/b".into()],
            refused: vec!["/etc".into()],
            keep: Map::new(),
        }
    }

    #[test]
    fn the_summary_names_mode_folders_and_trust() {
        let g = gw();
        assert_eq!(
            summary(&g, &g),
            "Allow my list · 2 allowed folders · 1 refused folder · launch folder trusted"
        );
        let mut all = g.clone();
        all.mode = Some("blacklist".into());
        all.trust = Some(false);
        assert_eq!(
            summary(&all, &all),
            "Allow everything except · 1 refused folder · launch folder not trusted"
        );
    }

    #[test]
    fn an_account_inherits_what_it_does_not_set_and_keeps_the_gateway_refusals() {
        let g = gw();
        let acc = Policy::from_entry(&json!({"workspace_allowed_paths": ["/home/al"]}));
        assert_eq!(
            summary(&acc, &g),
            "Allow my list · 1 allowed folder · 1 refused folder · launch folder trusted"
        );
        assert_eq!(account_cell(None, &g), "Follows the gateway policy");
        assert!(account_cell(Some(&acc), &g).starts_with("Own policy · "));
    }

    #[test]
    fn the_entry_keeps_the_admin_grant_and_drops_unset_fields() {
        let p = Policy::from_entry(&json!({
            "mode": "blacklist", "client_workspace_scope_overrides": true,
            "workspace_blocked_paths": "/x\n/y"
        }));
        assert_eq!(
            p.entry(),
            json!({"mode": "blacklist", "client_workspace_scope_overrides": true,
                   "workspace_blocked_paths": ["/x", "/y"]})
        );
        assert!(!Policy::default().is_custom());
    }

    #[test]
    fn path_check_reads_the_gateway_answer() {
        let c = PathCheck::from_value(
            &json!({"path": "~/x", "normalized": "/Users/a/x", "absolute": true, "exists": false,
                    "is_dir": false, "valid": true, "sentence": "This folder does not exist yet."}),
            "~/x",
        );
        assert!(c.valid && !c.exists);
        assert_eq!(c.normalized, "/Users/a/x");
        assert_eq!(c.sentence, "This folder does not exist yet.");
    }

    #[test]
    fn mode_words_match_the_segmented_switch() {
        assert_eq!(mode_label("whitelist"), MODE_ALLOW_LIST);
        assert_eq!(mode_label("blacklist"), MODE_ALLOW_ALL);
    }
}
