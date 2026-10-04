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

/// The access mode's two segments (the web's segmented switch) and its
/// field help — the web console's words (console_workspaces.py).
pub const MODE_LABEL: &str = "Access";
pub const MODE_HELP: &str = "Which folders agents may use.";
pub const MODE_ALLOW_LIST: &str = "Allow my list";
pub const MODE_ALLOW_ALL: &str = "Allow everything except";
pub const MODE_HELP_LIST: &str = "Only the allowed folders below.";
pub const MODE_HELP_ALL: &str = "Any folder except the refused ones.";
pub const TRUST_LABEL: &str = "Launch-folder trust";
pub const TRUST_HELP: &str = "Agents may also use the folder they were started from.";
pub const ALLOWED_TITLE: &str = "Allowed folders";
pub const REFUSED_TITLE: &str = "Refused folders";
pub const ADD_FOLDER: &str = "+ Add folder";
pub const ROOT_LABEL: &str = "Default folder";
pub const ROOT_HELP: &str =
    "Where a run starts when the app names no folder; empty = the gateway's own folder.";
pub const LEGACY_LABEL: &str = "Any folder (old clients)";
pub const LEGACY_HELP: &str = "Lets old clients name any folder; the rules above stop applying.";
pub const OWN_LABEL: &str = "Own policy";
pub const GATEWAY_NOTE: &str = "Every account follows it unless it has its own policy below.";
pub const ACCOUNTS_NOTE: &str = "Turn on Own policy to give one account different folders.";
pub const SELF_NOTE: &str =
    "Turn on Own policy to choose your own folders; the gateway's refused folders always apply.";
pub const ENTITIES_NOTE: &str = "Entities: their folders are set in Manage, on the Accounts page.";
pub const DUPLICATE: &str = "Already in this list. Not saved.";
pub const TITLE: &str = "Workspaces";
pub const SUBTITLE: &str = "Which folders agents may read and write";

/// The folder lists' help, per scope (the web's).
pub fn list_help(scope: &Scope, kind: ListKind) -> &'static str {
    match (scope, kind) {
        (Scope::Gateway, ListKind::Allowed) => "Folders every account may use.",
        (Scope::Gateway, ListKind::Refused) => "Folders no agent may ever use, in either mode.",
        (Scope::Account { .. }, ListKind::Allowed) => {
            "Folders this account may use, on top of the gateway's allowed folders."
        }
        (Scope::Account { .. }, ListKind::Refused) => {
            "Folders this account may never use, on top of the gateway's refused folders."
        }
        (Scope::Own, ListKind::Allowed) => {
            "Folders your agents may use, on top of the gateway's allowed folders."
        }
        (Scope::Own, ListKind::Refused) => "Folders your agents may never use.",
    }
}

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
    /// "Any folder (old clients)" (`client_workspace_scope_overrides`);
    /// None = not set (accounts).
    pub legacy: Option<bool>,
    /// The gateway's "Default folder" (`workspace_root`; gateway only):
    /// the SAVED value ("" = the gateway's own folder), and the folder in
    /// use for the placeholder.
    pub root: Option<String>,
    pub root_in_use: String,
    /// Fields the editor never shows but a rewrite must keep.
    pub keep: Map<String, Value>,
}

impl Policy {
    /// A stored per-account entry (`user_workspace_policies["t:u"]` or the
    /// self route's `policy`).
    pub fn from_entry(v: &Value) -> Policy {
        let keep = Map::new();
        Policy {
            legacy: v
                .get("client_workspace_scope_overrides")
                .and_then(Value::as_bool),
            root: None,
            root_in_use: String::new(),
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
            legacy: Some(c.client_workspace_scope_overrides),
            root: Some(if c.workspace_root_source == "stored" {
                c.workspace_root.clone()
            } else {
                String::new()
            }),
            root_in_use: c.workspace_root.clone(),
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
        // The web removes the grant when switched off (never writes false).
        if self.legacy == Some(true) {
            m.insert("client_workspace_scope_overrides".into(), json!(true));
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

/// The gateway policy in one line (the web's `wsGatewaySentence`).
pub fn gateway_sentence(g: &Policy) -> String {
    if g.legacy == Some(true) {
        return "Any folder (old clients) is on: agents may use any folder the app names; the folder rules don't apply.".into();
    }
    if g.mode.as_deref() == Some("blacklist") {
        return format!(
            "Agents may use any folder except {}.",
            if g.refused.is_empty() {
                "none refused yet".to_string()
            } else {
                plural(g.refused.len(), "refused folder", "refused folders")
            }
        );
    }
    let mut what: Vec<String> = Vec::new();
    if !g.allowed.is_empty() {
        what.push(plural(g.allowed.len(), "allowed folder", "allowed folders"));
    }
    if g.trust.unwrap_or(true) {
        what.push("the folder they start in".into());
    }
    if what.is_empty() {
        return "Agents may not use any folder yet: add an allowed folder or turn on launch-folder trust.".into();
    }
    let refused = if g.refused.is_empty() {
        String::new()
    } else {
        format!("; {} refused", plural(g.refused.len(), "folder", "folders"))
    };
    format!("Agents may use only {}{refused}.", what.join(" and "))
}

/// An account's policy in one line (the web's `wsOwnSentence`).
pub fn own_sentence(entry: Option<&Policy>, g: &Policy) -> String {
    let Some(e) = entry.filter(|e| e.is_custom()) else {
        return "Follows the gateway policy.".into();
    };
    if e.legacy == Some(true) {
        return "Any folder (old clients) is on for this account.".into();
    }
    let mode = e.effective_mode(g);
    let trust = e.effective_trust(g);
    let blocked = e.refused.len();
    if mode == "blacklist" {
        return format!(
            "Any folder except {}.",
            if blocked > 0 {
                plural(blocked, "refused folder", "refused folders")
            } else {
                "the gateway's refused ones".to_string()
            }
        );
    }
    let mut parts = vec!["the gateway's allowed folders".to_string()];
    if !e.allowed.is_empty() {
        parts.push(plural(
            e.allowed.len(),
            "folder of its own",
            "folders of its own",
        ));
    }
    if trust {
        parts.push("the folder it starts in".into());
    }
    let refused = if blocked > 0 {
        format!("; {} refused", plural(blocked, "folder", "folders"))
    } else {
        String::new()
    };
    format!("Only {}{refused}.", parts.join(", "))
}

/// The page's summary line for an admin: the gateway sentence, then how
/// many accounts have their own policy (the web's `wsSummaryText`).
pub fn admin_summary(g: &Policy, own: usize) -> String {
    let tail = match own {
        0 => String::new(),
        1 => " 1 account has its own policy.".into(),
        n => format!(" {n} accounts have their own policy."),
    };
    format!("{}{tail}", gateway_sentence(g))
}

/// A non-admin's summary line (the web's `wsSummaryText`, self part).
pub fn self_summary(mine: &Policy, customized: bool, eff_mode: &str, eff_trust: bool) -> String {
    let who = if customized {
        "Your own policy"
    } else {
        "The gateway policy"
    };
    let refused = if customized { mine.refused.len() } else { 0 };
    if eff_mode == "blacklist" {
        let tail = if refused > 0 {
            format!(" ({refused} of yours)")
        } else {
            String::new()
        };
        return format!("{who}: your agents may use any folder except the refused ones{tail}.");
    }
    let mut parts = vec!["the gateway's allowed folders".to_string()];
    let extra = if customized { mine.allowed.len() } else { 0 };
    if extra > 0 {
        parts.push(plural(extra, "folder of yours", "folders of yours"));
    }
    if eff_trust {
        parts.push("the folder they start in".into());
    }
    let tail = if refused > 0 {
        format!("; {} refused", plural(refused, "folder", "folders"))
    } else {
        String::new()
    };
    format!(
        "{who}: your agents may use only {}{tail}.",
        parts.join(", ")
    )
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
    /// The gateway's Default folder.
    Root,
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
        }
    }

    /// Forget the previous gateway's state (a reconnect).
    pub fn reset(&self) {
        self.editing.set(None);
        self.msg.set(None);
        self.busy.set(false);
        self.focus.set(None);
    }
}

/// The reactive scope type (the page's `Scope` enum shadows the name).
pub type Scope_ = abstracttui::prelude::Scope;

#[cfg(test)]
mod tests {
    use super::*;

    fn gw() -> Policy {
        Policy {
            mode: Some("whitelist".into()),
            trust: Some(true),
            allowed: vec!["/srv/a".into(), "/srv/b".into()],
            refused: vec!["/etc".into()],
            legacy: Some(false),
            ..Policy::default()
        }
    }

    #[test]
    fn the_gateway_sentence_is_the_webs() {
        let g = gw();
        assert_eq!(
            gateway_sentence(&g),
            "Agents may use only 2 allowed folders and the folder they start in; 1 folder refused."
        );
        let mut all = g.clone();
        all.mode = Some("blacklist".into());
        assert_eq!(
            gateway_sentence(&all),
            "Agents may use any folder except 1 refused folder."
        );
        let none = Policy {
            mode: Some("whitelist".into()),
            trust: Some(false),
            ..Policy::default()
        };
        assert_eq!(
            gateway_sentence(&none),
            "Agents may not use any folder yet: add an allowed folder or turn on launch-folder trust."
        );
        assert!(admin_summary(&g, 2).ends_with(" 2 accounts have their own policy."));
    }

    #[test]
    fn an_accounts_sentence_is_the_webs() {
        let g = gw();
        assert_eq!(own_sentence(None, &g), "Follows the gateway policy.");
        let e = Policy::from_entry(&json!({"workspace_allowed_paths": ["/home/al"]}));
        assert_eq!(
            own_sentence(Some(&e), &g),
            "Only the gateway's allowed folders, 1 folder of its own, the folder it starts in."
        );
        let l = Policy::from_entry(&json!({"client_workspace_scope_overrides": true}));
        assert_eq!(
            own_sentence(Some(&l), &g),
            "Any folder (old clients) is on for this account."
        );
    }

    #[test]
    fn the_entry_keeps_the_grant_only_when_on() {
        let p = Policy::from_entry(&json!({
            "mode": "blacklist", "client_workspace_scope_overrides": true,
            "workspace_blocked_paths": "/x\n/y"
        }));
        assert_eq!(
            p.entry(),
            json!({"mode": "blacklist", "client_workspace_scope_overrides": true,
                   "workspace_blocked_paths": ["/x", "/y"]})
        );
        let mut off = p.clone();
        off.legacy = Some(false);
        assert!(off
            .entry()
            .get("client_workspace_scope_overrides")
            .is_none());
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
    }

    #[test]
    fn the_self_summary_is_the_webs() {
        let mine = Policy::default();
        assert_eq!(
            self_summary(&mine, false, "whitelist", true),
            "The gateway policy: your agents may use only the gateway's allowed folders, the folder they start in."
        );
    }
}
