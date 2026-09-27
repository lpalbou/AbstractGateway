//! Operator-controls state (web-console parity): the gateway host card
//! (runner pause / restart / quit / self-update / desktop tray), the
//! "Workflows are paused" banner, the caller's own workspace policy and
//! the backlog settings rows of the runtime knobs.
//!
//! Declared as `store::operator` (one `mod` line in store.rs) so the
//! shared store file only grows by one field. Parsing is pure and
//! unit-tested here; nothing does I/O.

use abstracttui::prelude::*;
use serde_json::Value;

use super::Loadable;

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}

fn strs(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default()
}

/// The signals behind the operator controls (Copy: all signals).
#[derive(Clone, Copy)]
pub struct OperatorStore {
    /// `GET /host/runner` — execution state of the gateway process.
    pub runner: Signal<Loadable<HostRunner>>,
    /// `GET /host/tray` rendered as the web console's one-line note.
    pub tray: Signal<Loadable<String>>,
    /// `GET /host/update` (admin) — install kind, last check, upgrade job.
    pub update: Signal<Loadable<HostUpdate>>,
    /// The restart / quit story after the gateway accepted one: the
    /// connection is EXPECTED to drop, and this line says what the
    /// console is waiting for and what it saw. Survives the domain reset
    /// a reconnect performs (it is the record of why the gateway left).
    pub lifecycle: Signal<Option<String>>,
    /// Generation gate for the 15 s `/host/runner` poll behind the
    /// paused banner (the host-state poll precedent).
    pub runner_poll_gen: Signal<u64>,
    /// `GET /workspace/policy/self` — the caller's own policy.
    pub my_policy: Signal<Loadable<MyPolicy>>,
}

impl OperatorStore {
    pub fn create(cx: Scope) -> OperatorStore {
        OperatorStore {
            runner: cx.signal(Loadable::default()),
            tray: cx.signal(Loadable::default()),
            update: cx.signal(Loadable::default()),
            lifecycle: cx.signal(None),
            runner_poll_gen: cx.signal(0),
            my_policy: cx.signal(Loadable::default()),
        }
    }

    /// A new gateway / principal: every remote answer is unvouched-for.
    /// `lifecycle` survives (the restart story spans the reconnect);
    /// the poll generation is bumped so a live chain dies with the world
    /// it was reading.
    pub fn reset(&self) {
        self.runner.set(Loadable::NotAsked);
        self.tray.set(Loadable::NotAsked);
        self.update.set(Loadable::NotAsked);
        self.my_policy.set(Loadable::NotAsked);
        self.runner_poll_gen.update(|g| *g += 1);
    }
}

// ---------------------------------------------------------------------
// Host runner (`GET /host/runner`, and the body of pause/resume answers)
// ---------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct HostRunner {
    pub paused: bool,
    pub paused_at: String,
    pub paused_by: String,
    pub reason: String,
    pub inflight_ticks: u64,
    /// None = the payload did not say.
    pub runner_in_process: Option<bool>,
    pub cap_restart: bool,
    pub cap_shutdown: bool,
    /// Why restart/shutdown are unavailable (gateway's words).
    pub cap_reason: String,
    pub restart_requested: bool,
    pub shutdown_requested: bool,
}

impl HostRunner {
    pub fn from_value(v: &Value) -> HostRunner {
        let caps = v.get("capabilities").cloned().unwrap_or(Value::Null);
        let flag = |x: &Value, k: &str| x.get(k).and_then(Value::as_bool).unwrap_or(false);
        HostRunner {
            paused: flag(v, "paused"),
            paused_at: s(v, "paused_at").unwrap_or_default(),
            paused_by: s(v, "paused_by").unwrap_or_default(),
            reason: s(v, "reason").unwrap_or_default(),
            inflight_ticks: v.get("inflight_ticks").and_then(Value::as_u64).unwrap_or(0),
            runner_in_process: v.get("runner_in_process").and_then(Value::as_bool),
            cap_restart: flag(&caps, "restart"),
            cap_shutdown: flag(&caps, "shutdown"),
            cap_reason: s(&caps, "reason").unwrap_or_default(),
            restart_requested: flag(&caps, "restart_requested"),
            shutdown_requested: flag(&caps, "shutdown_requested"),
        }
    }

    /// The web console's state pill, word for word.
    pub fn state_text(&self) -> String {
        let n = self.inflight_ticks;
        if self.paused {
            if n > 0 {
                "Pausing…".into()
            } else {
                "Paused — still running".into()
            }
        } else if n > 0 {
            format!("Running · working on {n} step{}", if n == 1 { "" } else { "s" })
        } else {
            "Running".into()
        }
    }

    /// The detail line under the pill (web parity).
    pub fn detail_text(&self) -> String {
        if self.paused {
            [
                when_text(&self.paused_at),
                if self.paused_by.is_empty() {
                    String::new()
                } else {
                    format!("by {}", self.paused_by)
                },
                self.reason.clone(),
            ]
            .into_iter()
            .filter(|x| !x.is_empty())
            .collect::<Vec<_>>()
            .join(" · ")
        } else if self.runner_in_process == Some(false) {
            "Workflows run in a separate runner process; pausing reaches it through the shared data folder."
                .into()
        } else {
            String::new()
        }
    }
}

/// An ISO timestamp as the operator reads it: date + time to the second,
/// UTC offset kept (the console never guesses a time zone).
pub fn when_text(iso: &str) -> String {
    let iso = iso.trim();
    if iso.len() < 19 || !iso.is_char_boundary(19) {
        return iso.to_string();
    }
    let (head, rest) = iso.split_at(19);
    let head = head.replacen('T', " ", 1);
    // Drop fractional seconds, keep the offset ("+00:00" / "Z").
    let tail = rest.trim_start_matches(|c: char| c == '.' || c.is_ascii_digit());
    if tail.is_empty() {
        head
    } else {
        format!("{head} {tail}")
    }
}

/// The paused banner's sentence (web `renderPausedBanner`), or None when
/// the runner is not paused.
pub fn paused_banner_text(r: &HostRunner) -> Option<String> {
    if !r.paused {
        return None;
    }
    let when = if r.paused_at.is_empty() {
        String::new()
    } else {
        format!(" since {}", when_text(&r.paused_at))
    };
    let by = if r.paused_by.is_empty() {
        String::new()
    } else {
        format!(" by {}", r.paused_by)
    };
    Some(format!(
        "Workflows are paused{when}{by} — the gateway keeps answering; nothing new runs until you resume."
    ))
}

// ---------------------------------------------------------------------
// Desktop tray (`GET /host/tray`) → the web console's note
// ---------------------------------------------------------------------

pub fn tray_note(v: &Value) -> String {
    let null = Value::Null;
    let sup = v.get("supervisor").unwrap_or(&null);
    let dec = v.get("decision").unwrap_or(&null);
    let running = sup.get("running").and_then(Value::as_bool).unwrap_or(false);
    let ready = sup.get("ready").and_then(Value::as_bool);
    let reason = s(dec, "reason").unwrap_or_default();
    let failure = sup.get("failure").filter(|f| f.is_object());
    if running && ready != Some(false) {
        let pid = sup.get("pid").map(|p| p.to_string()).unwrap_or_else(|| "?".into());
        return format!("shown (pid {pid})");
    }
    if running {
        return "starting…".into();
    }
    match reason.as_str() {
        "missing_dependency" => {
            return format!("not installed — {}", s(v, "install_hint").unwrap_or_default())
        }
        "headless" => {
            return format!(
                "not available here — {}",
                s(dec, "hint").filter(|h| !h.is_empty()).unwrap_or_else(|| "no display".into())
            )
        }
        "dev_reload" => return "not available while running with --reload".into(),
        "not_serving" => {
            return "not available (this process was not started with `abstractgateway serve`)".into()
        }
        _ => {}
    }
    if let Some(f) = failure.and_then(|f| s(f, "reason").filter(|r| !r.is_empty()).map(|r| (r, f))) {
        let (r, f) = f;
        return match s(f, "hint").filter(|h| !h.is_empty()) {
            Some(h) => format!("not running — {r} ({h})"),
            None => format!("not running — {r}"),
        };
    }
    if let Some(e) = s(sup, "error").filter(|e| !e.is_empty()) {
        return format!("not running — {e}");
    }
    // The web's last arm; the decision's own hint (e.g. "started with
    // --no-tray") rides along so the operator sees WHY.
    match s(dec, "hint").filter(|h| !h.is_empty()) {
        Some(h) if !reason.is_empty() => format!("not running ({reason}: {h})"),
        _ => "not running".into(),
    }
}

// ---------------------------------------------------------------------
// Self-update (`GET /host/update`, `POST /host/update/check|start`)
// ---------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct HostUpdate {
    pub current: String,
    pub install_kind: String,
    pub upgradable: bool,
    pub install_reason: String,
    /// A check has run (the `check` block is an object).
    pub checked: bool,
    pub offline: bool,
    pub update_available: bool,
    pub latest: String,
    pub checked_at: String,
    pub job_state: String,
    pub job_error: String,
    pub job_last_log: String,
    pub job_version_after: String,
    pub restart_pending: bool,
}

impl HostUpdate {
    pub fn from_value(v: &Value) -> HostUpdate {
        let null = Value::Null;
        let inst = v.get("install").unwrap_or(&null);
        let chk = v.get("check").filter(|c| c.is_object());
        let job = v.get("job").unwrap_or(&null);
        let cb = |k: &str| chk.and_then(|c| c.get(k)).and_then(Value::as_bool).unwrap_or(false);
        let cs = |k: &str| chk.and_then(|c| s(c, k)).unwrap_or_default();
        HostUpdate {
            current: s(v, "current").unwrap_or_else(|| "?".into()),
            install_kind: s(inst, "kind").unwrap_or_default(),
            upgradable: inst.get("upgradable").and_then(Value::as_bool).unwrap_or(false),
            install_reason: s(inst, "reason").unwrap_or_default(),
            checked: chk.is_some(),
            offline: cb("offline"),
            update_available: cb("update_available"),
            latest: cs("latest"),
            checked_at: cs("checked_at"),
            job_state: s(job, "state").unwrap_or_default(),
            job_error: s(job, "error").unwrap_or_default(),
            job_last_log: strs(job, "log_tail").last().cloned().unwrap_or_default(),
            job_version_after: s(job, "version_after").unwrap_or_default(),
            restart_pending: v.get("restart_pending").and_then(Value::as_bool).unwrap_or(false),
        }
    }

    /// The version line (web `renderGatewayUpdate`, same precedence).
    pub fn version_text(&self) -> String {
        let mut text = self.current.clone();
        if self.job_state == "running" {
            text.push_str(" · installing…");
            if !self.job_last_log.is_empty() {
                text.push_str(&format!(" ({})", self.job_last_log));
            }
        } else if self.job_state == "succeeded" || self.restart_pending {
            let what = if self.job_version_after.is_empty() {
                "the update".to_string()
            } else {
                self.job_version_after.clone()
            };
            text.push_str(&format!(" · {what} is installed — restart to finish"));
        } else if self.job_state == "failed" {
            let why = if self.job_error.is_empty() { "see logs" } else { self.job_error.as_str() };
            text.push_str(&format!(" · the update didn't finish ({why})"));
        } else if self.job_state == "succeeded_no_change" {
            let why = if self.job_error.is_empty() { "nothing changed" } else { self.job_error.as_str() };
            text.push_str(&format!(" · {why}"));
        } else if self.checked && self.offline {
            text.push_str(" · couldn't reach the update server (offline?)");
        } else if self.checked && self.update_available {
            text.push_str(&format!(" · {} available", self.latest));
        } else if self.checked && !self.latest.is_empty() {
            text.push_str(&format!(" · up to date, checked {}", when_text(&self.checked_at)));
        } else if !self.checked {
            text.push_str(" · not checked yet");
        }
        text
    }

    /// Whether "Update to X" is offered (web `canStart`).
    pub fn can_start(&self) -> bool {
        self.update_available && self.upgradable && self.job_state != "running"
    }

    /// The hint under the version line (web parity).
    pub fn hint_text(&self) -> String {
        if self.update_available && !self.upgradable {
            self.install_reason.clone()
        } else if !self.install_kind.is_empty() {
            format!("installed with {}", self.install_kind)
        } else {
            String::new()
        }
    }
}

// ---------------------------------------------------------------------
// The caller's own workspace policy (`GET/PUT /workspace/policy/self`)
// ---------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct MyPolicy {
    pub tenant_id: String,
    pub user_id: String,
    /// The stored entry's fields ("" = not set / inherit).
    pub mode: String,
    /// "on" | "off" | "" (inherit).
    pub trust: String,
    pub allowed: Vec<String>,
    pub blocked: Vec<String>,
    pub customized: bool,
    pub eff_mode: String,
    pub eff_trust: bool,
    pub eff_allowed: usize,
    pub eff_blocked: usize,
}

impl MyPolicy {
    pub fn from_value(v: &Value) -> MyPolicy {
        let null = Value::Null;
        let entry = v.get("policy").filter(|p| p.is_object()).unwrap_or(&null);
        let eff = v.get("effective").unwrap_or(&null);
        MyPolicy {
            tenant_id: s(v, "tenant_id").unwrap_or_default(),
            user_id: s(v, "user_id").unwrap_or_default(),
            mode: s(entry, "mode").unwrap_or_default(),
            trust: match entry.get("trust_client_launch_folder").and_then(Value::as_bool) {
                Some(true) => "on".into(),
                Some(false) => "off".into(),
                None => String::new(),
            },
            allowed: strs(entry, "workspace_allowed_paths"),
            blocked: strs(entry, "workspace_blocked_paths"),
            customized: v.get("customized").and_then(Value::as_bool).unwrap_or(false),
            eff_mode: s(eff, "mode").unwrap_or_else(|| "whitelist".into()),
            eff_trust: eff.get("trust_client_launch_folder").and_then(Value::as_bool) == Some(true),
            eff_allowed: strs(eff, "workspace_allowed_paths").len(),
            eff_blocked: strs(eff, "workspace_blocked_paths").len(),
        }
    }

    /// The web console's "Effective: …" line.
    pub fn effective_text(&self) -> String {
        format!(
            "Effective: {} mode · launch-folder trust {} · {} allowed · {} refused",
            self.eff_mode,
            if self.eff_trust { "on" } else { "off" },
            self.eff_allowed,
            self.eff_blocked
        )
    }
}

/// The PUT body the web console sends (`saveMyWorkspacePolicy`): only the
/// fields the user set; lists are one path per line, blanks dropped. `{}`
/// clears the entry back to inherited.
pub fn my_policy_body(mode: &str, trust: &str, allowed: &str, blocked: &str) -> Value {
    let mut body = serde_json::Map::new();
    if !mode.is_empty() {
        body.insert("mode".into(), Value::String(mode.to_string()));
    }
    if !trust.is_empty() {
        body.insert("trust_client_launch_folder".into(), Value::Bool(trust == "on"));
    }
    let list = |raw: &str| -> Vec<Value> {
        raw.lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(|l| Value::String(l.to_string()))
            .collect()
    };
    let al = list(allowed);
    if !al.is_empty() {
        body.insert("workspace_allowed_paths".into(), Value::Array(al));
    }
    let bl = list(blocked);
    if !bl.is_empty() {
        body.insert("workspace_blocked_paths".into(), Value::Array(bl));
    }
    Value::Object(body)
}

// ---------------------------------------------------------------------
// Backlog settings (`triage_repo_root`, `backlog_exec_runner`,
// `process_manager` in GET /admin/runtime-config)
// ---------------------------------------------------------------------

/// The three keys, in the web console's order (`BACKLOG_SET_KEYS`).
pub const BACKLOG_KEYS: [&str; 3] = ["triage_repo_root", "backlog_exec_runner", "process_manager"];

#[derive(Clone, Debug, Default, PartialEq)]
pub struct BacklogSetting {
    pub key: String,
    pub label: String,
    pub help: String,
    pub cli: String,
    pub flag: String,
    /// flag | stored | env | default
    pub source: String,
    /// The value in use (folder path, or "on"/"off"); "" when redacted.
    pub value: String,
    /// The SAVED value the edit field starts from: the folder text, or
    /// "on" / "off" / "" (not saved) for a switch.
    pub saved: String,
    /// Folder row only: false = not usable, `reason` says why.
    pub available: Option<bool>,
    pub reason: String,
    pub default_path: String,
    /// Non-admin view of the folder: posture without the path.
    pub redacted: bool,
}

impl BacklogSetting {
    pub fn is_folder(&self) -> bool {
        self.key == "triage_repo_root"
    }
}

fn switch_word(v: Option<&Value>) -> String {
    match v.and_then(Value::as_bool) {
        Some(true) => "on".into(),
        Some(false) => "off".into(),
        None => String::new(),
    }
}

/// Parse the backlog rows present in a runtime-config payload (an older
/// gateway without them yields an empty list, never invented rows).
pub fn backlog_settings_from(v: &Value) -> Vec<BacklogSetting> {
    BACKLOG_KEYS
        .iter()
        .filter_map(|key| {
            let r = v.get(*key).filter(|r| r.is_object())?;
            let source = s(r, "source").unwrap_or_else(|| "default".into());
            let folder = *key == "triage_repo_root";
            let (value, saved) = if folder {
                let value = s(r, "value").unwrap_or_default();
                let saved = if source == "stored" {
                    value.clone()
                } else {
                    s(r, "stored_value").unwrap_or_default()
                };
                (value, saved)
            } else {
                let value = if r.get("value").and_then(Value::as_bool) == Some(true) {
                    "on".to_string()
                } else {
                    "off".to_string()
                };
                let saved = if source == "stored" {
                    switch_word(r.get("value"))
                } else {
                    switch_word(r.get("stored_value"))
                };
                (value, saved)
            };
            Some(BacklogSetting {
                key: key.to_string(),
                label: s(r, "label").unwrap_or_else(|| key.to_string()),
                help: s(r, "help").unwrap_or_default(),
                cli: s(r, "cli").unwrap_or_default(),
                flag: s(r, "flag").unwrap_or_default(),
                source,
                value,
                saved,
                available: r.get("available").and_then(Value::as_bool),
                reason: s(r, "reason").unwrap_or_default(),
                default_path: s(r, "default_path").unwrap_or_default(),
                redacted: folder && r.get("value").is_none(),
            })
        })
        .collect()
}

/// The source word the web console's pill shows.
pub fn backlog_source_word(source: &str) -> &'static str {
    match source {
        "flag" => "launch flag",
        "stored" => "saved setting",
        "env" => "environment (legacy)",
        _ => "default",
    }
}

/// The POST body the web console's Save sends (`backlogSettingsSave`):
/// only the keys whose edit differs from the SAVED value; an emptied
/// folder / "not saved" switch sends null (= back to flag/default).
/// `typed` = (key, text) where a switch's text is "on" / "off" / "".
pub fn backlog_settings_body(current: &[BacklogSetting], typed: &[(String, String)]) -> Value {
    let mut body = serde_json::Map::new();
    for (key, text) in typed {
        let Some(cur) = current.iter().find(|c| &c.key == key) else {
            continue;
        };
        let now = text.trim();
        if now == cur.saved {
            continue;
        }
        let v = if cur.is_folder() {
            if now.is_empty() {
                Value::Null
            } else {
                Value::String(now.to_string())
            }
        } else if now.is_empty() {
            Value::Null
        } else {
            Value::Bool(now == "on")
        };
        body.insert(key.clone(), v);
    }
    Value::Object(body)
}

/// The skills reseed report in the web console's words (`seedReportText`).
pub fn seed_report_text(rep: &Value) -> String {
    let n = |k: &str| rep.get(k).and_then(Value::as_array).map(Vec::len).unwrap_or(0);
    let kept = rep.get("kept").and_then(Value::as_object).map(|m| m.len()).unwrap_or(0);
    format!(
        "Curated shelf {}: {} added, {} updated, {} unchanged, {} kept as they are (your edits are never overwritten).",
        s(rep, "bundled_version").unwrap_or_else(|| "?".into()),
        n("added"),
        n("updated"),
        n("unchanged"),
        kept
    )
}

/// Whether the network panel offers "Look up my public address" (web
/// parity: admin, internet mode configured or running, and no public
/// address already listed).
pub fn offers_public_lookup(d: &crate::store::NetworkData) -> bool {
    d.writable
        && (d.configured_mode == "internet" || d.effective_mode == "internet")
        && !d.addresses.iter().any(|a| a.kind == "public")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn runner_state_and_detail_follow_the_web_wording() {
        let r = HostRunner::from_value(&json!({
            "paused": false, "inflight_ticks": 2, "runner_in_process": true,
            "capabilities": {"restart": true, "shutdown": true, "reason": null}
        }));
        assert_eq!(r.state_text(), "Running · working on 2 steps");
        assert_eq!(r.detail_text(), "");
        assert!(r.cap_restart && r.cap_shutdown);
        let p = HostRunner::from_value(&json!({
            "paused": true, "paused_at": "2026-09-27T15:36:02.442111+00:00",
            "paused_by": "default/admin", "reason": "maintenance", "inflight_ticks": 0,
            "capabilities": {"restart": false, "shutdown": true, "reason": "serve --reload"}
        }));
        assert_eq!(p.state_text(), "Paused — still running");
        assert_eq!(p.detail_text(), "2026-09-27 15:36:02 +00:00 · by default/admin · maintenance");
        assert!(!p.cap_restart);
        assert_eq!(p.cap_reason, "serve --reload");
        let split = HostRunner::from_value(&json!({"paused": false, "runner_in_process": false}));
        assert!(split.detail_text().contains("separate runner process"));
    }

    #[test]
    fn paused_banner_only_when_paused() {
        let r = HostRunner::from_value(&json!({"paused": false}));
        assert_eq!(paused_banner_text(&r), None);
        let p = HostRunner::from_value(&json!({
            "paused": true, "paused_at": "2026-09-27T10:00:00Z", "paused_by": "default/admin"
        }));
        assert_eq!(
            paused_banner_text(&p).unwrap(),
            "Workflows are paused since 2026-09-27 10:00:00 Z by default/admin — the gateway keeps answering; nothing new runs until you resume."
        );
    }

    #[test]
    fn tray_note_names_why_it_is_not_running() {
        let v = json!({"decision": {"start": false, "reason": "no_tray_flag", "hint": "started with --no-tray"},
                       "supervisor": {"running": false}});
        assert_eq!(tray_note(&v), "not running (no_tray_flag: started with --no-tray)");
        let v = json!({"decision": {"reason": "missing_dependency"}, "install_hint": "pip install x",
                       "supervisor": {"running": false}});
        assert_eq!(tray_note(&v), "not installed — pip install x");
        let v = json!({"supervisor": {"running": true, "ready": true, "pid": 42}});
        assert_eq!(tray_note(&v), "shown (pid 42)");
    }

    #[test]
    fn update_line_follows_the_web_precedence() {
        let editable = HostUpdate::from_value(&json!({
            "current": "0.5.1", "install": {"kind": "editable", "upgradable": false, "reason": "git pull"},
            "check": null, "job": {"state": "idle", "log_tail": []}
        }));
        assert_eq!(editable.version_text(), "0.5.1 · not checked yet");
        assert_eq!(editable.hint_text(), "installed with editable");
        assert!(!editable.can_start());
        let avail = HostUpdate::from_value(&json!({
            "current": "0.5.1", "install": {"kind": "pip", "upgradable": true},
            "check": {"update_available": true, "latest": "0.6.0", "checked_at": "2026-09-27T10:00:00Z"},
            "job": {"state": "idle"}
        }));
        assert_eq!(avail.version_text(), "0.5.1 · 0.6.0 available");
        assert!(avail.can_start());
        let blocked = HostUpdate { upgradable: false, install_reason: "editable".into(), ..avail.clone() };
        assert!(!blocked.can_start());
        assert_eq!(blocked.hint_text(), "editable");
        let running = HostUpdate { job_state: "running".into(), job_last_log: "Collecting".into(), ..avail };
        assert_eq!(running.version_text(), "0.5.1 · installing… (Collecting)");
        assert!(!running.can_start());
        let offline = HostUpdate::from_value(&json!({"current": "1", "check": {"offline": true}}));
        assert_eq!(offline.version_text(), "1 · couldn't reach the update server (offline?)");
    }

    #[test]
    fn my_policy_parses_and_builds_the_web_body() {
        let p = MyPolicy::from_value(&json!({
            "tenant_id": "default", "user_id": "admin",
            "policy": {"mode": "blacklist", "trust_client_launch_folder": false,
                       "workspace_blocked_paths": ["/a", "/b"]},
            "customized": true,
            "effective": {"mode": "blacklist", "trust_client_launch_folder": false,
                          "workspace_allowed_paths": [], "workspace_blocked_paths": ["/a", "/b"]}
        }));
        assert_eq!(p.mode, "blacklist");
        assert_eq!(p.trust, "off");
        assert_eq!(p.blocked, vec!["/a", "/b"]);
        assert_eq!(p.effective_text(), "Effective: blacklist mode · launch-folder trust off · 0 allowed · 2 refused");
        assert_eq!(my_policy_body("", "", "", ""), json!({}));
        assert_eq!(
            my_policy_body("whitelist", "on", " /x \n\n/y", ""),
            json!({"mode": "whitelist", "trust_client_launch_folder": true,
                   "workspace_allowed_paths": ["/x", "/y"]})
        );
    }

    fn backlog_payload() -> Value {
        json!({
            "writable": true,
            "triage_repo_root": {"source": "default", "value": "/d/backlog", "default_path": "/d/backlog",
                                 "available": true, "label": "Backlog folder", "cli": "c", "flag": "--backlog-root"},
            "backlog_exec_runner": {"value": false, "source": "default", "label": "Backlog exec runner"},
            "process_manager": {"value": true, "source": "stored", "label": "Process manager"}
        })
    }

    #[test]
    fn backlog_rows_parse_saved_values_like_the_web() {
        let rows = backlog_settings_from(&backlog_payload());
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].key, "triage_repo_root");
        assert_eq!(rows[0].saved, "", "a default folder is not a saved one");
        assert_eq!(rows[0].value, "/d/backlog");
        assert_eq!(rows[1].value, "off");
        assert_eq!(rows[1].saved, "");
        assert_eq!(rows[2].saved, "on");
        // Non-admin: the folder path is redacted.
        let red = backlog_settings_from(&json!({"triage_repo_root": {"source": "default", "configured": true}}));
        assert!(red[0].redacted);
        assert!(backlog_settings_from(&json!({"writable": true})).is_empty());
    }

    #[test]
    fn backlog_body_sends_only_changes_and_null_for_cleared() {
        let rows = backlog_settings_from(&backlog_payload());
        let typed = |f: &str, e: &str, p: &str| {
            vec![
                ("triage_repo_root".to_string(), f.to_string()),
                ("backlog_exec_runner".to_string(), e.to_string()),
                ("process_manager".to_string(), p.to_string()),
            ]
        };
        assert_eq!(backlog_settings_body(&rows, &typed("", "", "on")), json!({}));
        assert_eq!(
            backlog_settings_body(&rows, &typed("/repo", "on", "")),
            json!({"triage_repo_root": "/repo", "backlog_exec_runner": true, "process_manager": null})
        );
    }

    #[test]
    fn seed_report_counts_each_list() {
        let rep = json!({"bundled_version": "2026.09.25", "added": ["a"], "updated": [],
                         "unchanged": ["b", "c"], "kept": {"d": "edited"}});
        assert_eq!(
            seed_report_text(&rep),
            "Curated shelf 2026.09.25: 1 added, 0 updated, 2 unchanged, 1 kept as they are (your edits are never overwritten)."
        );
    }

    #[test]
    fn public_lookup_is_offered_like_the_web() {
        let mut d = crate::store::NetworkData {
            writable: true,
            configured_mode: "internet".into(),
            effective_mode: "localhost".into(),
            ..Default::default()
        };
        assert!(offers_public_lookup(&d));
        d.writable = false;
        assert!(!offers_public_lookup(&d), "admin only");
        d.writable = true;
        d.configured_mode = "lan".into();
        assert!(!offers_public_lookup(&d), "internet mode only");
        d.effective_mode = "internet".into();
        d.addresses.push(crate::store::NetworkAddress { kind: "public".into(), ..Default::default() });
        assert!(!offers_public_lookup(&d), "not when a public address is listed");
    }

    #[test]
    fn when_text_trims_fraction_keeps_offset() {
        assert_eq!(when_text("2026-09-27T15:36:02.442111+00:00"), "2026-09-27 15:36:02 +00:00");
        assert_eq!(when_text("2026-09-27T15:36:02"), "2026-09-27 15:36:02");
        assert_eq!(when_text("garbage"), "garbage");
    }
}
