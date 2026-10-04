//! Browser apps state (the Apps screen): typed rows parsed from
//! `GET /api/gateway/apps`, the jobs this console watches, the result
//! notes, and the pure decision logic — which verbs an app offers, in
//! which state, and WHY a verb is unavailable. It mirrors the web
//! console's Apps tab (console_ui.py `appCardMarkup` / `appTuiParts` /
//! `appRuntimeMarkup` / `appJobResult`) rule for rule, so both consoles
//! offer the same actions under the same guards.
//!
//! Declared as a child of `store` (`#[path]` in store.rs). Nothing here
//! does I/O.

use abstracttui::prelude::*;
use serde_json::Value;

use super::Loadable;
use crate::api::ApiError;

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|x| !x.is_empty())
}
fn b(v: &Value, key: &str) -> bool {
    v.get(key).and_then(Value::as_bool).unwrap_or(false)
}
fn u(v: &Value, key: &str) -> Option<u64> {
    v.get(key).and_then(Value::as_u64)
}
fn strs(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Job key of an app's own install/update job.
pub fn app_key(app_id: &str) -> String {
    format!("app:{app_id}")
}
/// Job key of an app's terminal-app install job.
pub fn tui_key(app_id: &str) -> String {
    format!("tui:{app_id}")
}
/// Job key of the Node.js runtime install.
pub const NODE_KEY: &str = "__node__";

// ---------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct JobError {
    pub reason: Option<String>,
    pub message: String,
    pub hint: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct JobPart {
    pub id: String,
    pub label: String,
    pub state: String,
}

/// One apps job (`Job.to_dict`): install, update, terminal install or
/// the Node.js runtime.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppJob {
    pub id: String,
    pub kind: String,
    pub app_id: Option<String>,
    pub title: String,
    /// queued | running | succeeded | failed | cancelled
    pub state: String,
    pub percent: f64,
    pub indeterminate: bool,
    pub bytes_done: u64,
    pub bytes_total: Option<u64>,
    pub message: String,
    pub details: Option<String>,
    pub error: Option<JobError>,
    pub parts: Vec<JobPart>,
    pub result_version: Option<String>,
    pub result_url: Option<String>,
    pub result_terminal: bool,
    pub log_tail: Vec<String>,
    pub log_path: Option<String>,
}

impl AppJob {
    pub fn from_value(v: &Value) -> Option<AppJob> {
        let id = s(v, "id")?;
        let error = match v.get("error") {
            Some(Value::Object(_)) => {
                let e = v.get("error").unwrap_or(&Value::Null);
                Some(JobError {
                    reason: s(e, "reason"),
                    message: s(e, "message").unwrap_or_default(),
                    hint: s(e, "hint"),
                })
            }
            Some(Value::String(m)) if !m.is_empty() => Some(JobError {
                reason: None,
                message: m.clone(),
                hint: None,
            }),
            _ => None,
        };
        let result = v.get("result").cloned().unwrap_or(Value::Null);
        Some(AppJob {
            id,
            kind: s(v, "kind").unwrap_or_default(),
            app_id: s(v, "app_id"),
            title: s(v, "title").unwrap_or_default(),
            state: s(v, "state").unwrap_or_default(),
            percent: v.get("percent").and_then(Value::as_f64).unwrap_or(0.0),
            indeterminate: b(v, "indeterminate"),
            bytes_done: u(v, "bytes_done").unwrap_or(0),
            bytes_total: u(v, "bytes_total").filter(|n| *n > 0),
            message: s(v, "message").unwrap_or_default(),
            details: s(v, "details"),
            error,
            parts: v
                .get("parts")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|p| JobPart {
                            id: s(p, "id").unwrap_or_default(),
                            label: s(p, "label").unwrap_or_default(),
                            state: s(p, "state").unwrap_or_default(),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            result_version: s(&result, "version"),
            result_url: s(&result, "url"),
            result_terminal: result
                .get("terminal")
                .map(|t| !t.is_null() && t != &Value::Bool(false))
                .unwrap_or(false),
            log_tail: strs(v, "log_tail"),
            log_path: s(v, "log_path"),
        })
    }

    /// queued or running: the web console's `appJobActive`.
    pub fn is_active(&self) -> bool {
        matches!(self.state.as_str(), "queued" | "running")
    }

    /// The whole log a finished job left (`details`, else the tail).
    pub fn log_text(&self) -> String {
        match &self.details {
            Some(d) => d.clone(),
            None => self.log_tail.join("\n"),
        }
    }

    /// One progress line: title · percent (or "working") · bytes · message.
    pub fn progress_line(&self, fallback_title: &str) -> String {
        let mut out = if self.title.is_empty() {
            fallback_title.to_string()
        } else {
            self.title.clone()
        };
        if self.state == "queued" {
            out.push_str(" · waiting to start");
        } else if self.indeterminate {
            out.push_str(" · working");
        } else {
            out.push_str(&format!(" · {:.0}%", self.percent));
        }
        if let Some(total) = self.bytes_total {
            out.push_str(&format!(
                " · {} / {}",
                human_bytes(self.bytes_done),
                human_bytes(total)
            ));
        }
        if !self.message.is_empty() {
            out.push_str(&format!(" · {}", self.message));
        }
        out
    }
}

pub fn human_bytes(n: u64) -> String {
    const MB: f64 = 1024.0 * 1024.0;
    if (n as f64) >= MB {
        format!("{:.1} MB", n as f64 / MB)
    } else if n >= 1024 {
        format!("{:.0} KB", n as f64 / 1024.0)
    } else {
        format!("{n} B")
    }
}

/// The words the web console prints for a job part's state.
pub fn part_word(state: &str) -> &str {
    match state {
        "waiting" => "Waiting",
        "running" => "Installing...",
        "done" => "Installed",
        "failed" => "Did not install",
        "cancelled" => "Cancelled",
        "skipped" => "Not started",
        other => other,
    }
}

/// `interfaces[]` entry of kind "tui" (a terminal version: Code).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TuiIface {
    pub name: String,
    pub binary: String,
    pub installed: bool,
    pub version: Option<String>,
    pub latest_version: Option<String>,
    pub update_available: bool,
    pub install_available: bool,
    /// npm | release_binary | cargo
    pub install_method: String,
    pub install_blocked_reason: Option<String>,
    pub install_command: Option<String>,
    pub launch_available: bool,
    pub launch_blocked_reason: Option<String>,
    /// terminal | copy
    pub launch_mode: String,
    pub command: Option<String>,
    pub signin_command: Option<String>,
    pub path: Option<String>,
    pub active_job: Option<AppJob>,
}

impl TuiIface {
    fn from_value(v: &Value) -> TuiIface {
        TuiIface {
            name: s(v, "name").unwrap_or_default(),
            binary: s(v, "binary").unwrap_or_default(),
            installed: b(v, "installed"),
            version: s(v, "version"),
            latest_version: s(v, "latest_version"),
            update_available: b(v, "update_available"),
            install_available: b(v, "install_available"),
            install_method: s(v, "install_method").unwrap_or_default(),
            install_blocked_reason: s(v, "install_blocked_reason"),
            install_command: s(v, "install_command"),
            launch_available: b(v, "launch_available"),
            launch_blocked_reason: s(v, "launch_blocked_reason"),
            launch_mode: s(v, "launch_mode").unwrap_or_default(),
            command: s(v, "command"),
            signin_command: s(v, "signin_command"),
            path: s(v, "path"),
            active_job: v.get("active_job").and_then(AppJob::from_value),
        }
    }
}

/// A desktop app's `desktop` block (the Assistant).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct DesktopInfo {
    pub location: Option<String>,
    pub launch_command: Option<String>,
    pub install_command: Option<String>,
    pub launch_available: bool,
    /// not_installed | other_computer | admin | None
    pub launch_blocked: Option<String>,
    pub launch_blocked_reason: Option<String>,
    /// R10.5: another Assistant (the other artifact) runs — its sentence.
    pub other_running: Option<String>,
    /// R10.6: an update left a running copy alone — "Quit it and open it
    /// again to run x.y.z" while that copy runs.
    pub restart_note: Option<String>,
    /// R10.6: PyPI could not be asked — why the latest version is unknown.
    pub latest_error: Option<String>,
}

/// The web card's one-line blurb (console_ui.py `APP_COPY`); an app the
/// web does not list keeps the gateway's own description.
pub fn app_blurb(row: &AppRow) -> String {
    match row.id.as_str() {
        "flow" => "Design workflows visually and run them here.",
        "code" => "A coding assistant whose sessions survive restarts.",
        "observer" => "Watch runs live, replay them, steer running work.",
        "continuum" => "Your backlog, inbox and long-running processes.",
        "entity" => "Create entities and talk with them as they learn.",
        "assistant" => "A menu-bar assistant: chat or talk hands-free.",
        _ => return row.description.clone(),
    }
    .to_string()
}

impl AppsOverview {
    /// The address apps open at: the gateway a browser reaches + the apps
    /// prefix (the web's `appBrowserOrigin() + apps_path_prefix`).
    pub fn apps_base(&self) -> String {
        let origin = self
            .browser_gateway_url
            .clone()
            .or_else(|| self.gateway_url.clone())
            .unwrap_or_default();
        format!(
            "{}{}",
            origin.trim_end_matches('/'),
            self.apps_path_prefix.as_deref().unwrap_or("/apps/")
        )
    }

    /// The page's intro sentence, the web's words.
    pub fn intro(&self) -> String {
        format!(
            "Apps open in your browser at {}…, already signed in to this gateway.",
            self.apps_base()
        )
    }

    /// The web card's "Address" (served through the gateway) for `row`.
    pub fn address_of(&self, row: &AppRow) -> Option<String> {
        let path = row.app_path.as_deref()?;
        let origin = self
            .browser_gateway_url
            .clone()
            .or_else(|| self.gateway_url.clone())?;
        Some(format!("{}{}", origin.trim_end_matches('/'), path))
    }
}

/// One app row (`apps_manager.app_row` / `desktop_row` / `_external_row`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppRow {
    pub id: String,
    pub name: String,
    /// web | desktop
    pub kind: String,
    pub description: String,
    pub package: String,
    pub installed: bool,
    pub version: Option<String>,
    pub latest_version: Option<String>,
    pub update_available: bool,
    /// The gateway's words for an available update (R10.6): the web
    /// console's button label and tooltip — the verb label and its
    /// confirmation here. On a row started outside the gateway the tip says
    /// where to update it (and there is no update action).
    pub update_label: Option<String>,
    pub update_tip: Option<String>,
    pub running: bool,
    /// not_installed | stopped | starting | running | stopping | crashed | crash_loop
    pub status: String,
    /// gateway | external | (desktop: where it was found) | None
    pub source: Option<String>,
    /// Started outside the gateway: (port, detail).
    pub external_port: Option<u64>,
    pub url: Option<String>,
    pub port: Option<u64>,
    pub pid: Option<u64>,
    pub last_error: Option<String>,
    pub needs_node_install: bool,
    pub install_available: bool,
    pub install_blocked_reason: Option<String>,
    pub install_parts: Vec<String>,
    pub actions: Vec<String>,
    pub active_job: Option<AppJob>,
    pub log_path: Option<String>,
    /// Where the gateway serves it (`/apps/<id>/`, app_proxy.py).
    pub app_path: Option<String>,
    /// Entity: `content_summary.entities_count` (None = unknown / not reported).
    pub entities_count: Option<u64>,
    pub tui: Option<TuiIface>,
    pub desktop: Option<DesktopInfo>,
}

impl AppRow {
    pub fn from_value(v: &Value) -> Option<AppRow> {
        let id = s(v, "id")?;
        let tui = v
            .get("interfaces")
            .and_then(Value::as_array)
            .and_then(|a| {
                a.iter()
                    .find(|i| i.get("kind").and_then(Value::as_str) == Some("tui"))
            })
            .map(TuiIface::from_value);
        let desktop = v
            .get("desktop")
            .filter(|d| d.is_object())
            .map(|d| DesktopInfo {
                location: s(d, "location"),
                launch_command: s(d, "launch_command"),
                install_command: s(d, "install_command"),
                launch_available: b(d, "launch_available"),
                launch_blocked: s(d, "launch_blocked"),
                launch_blocked_reason: s(d, "launch_blocked_reason"),
                other_running: d.get("other_running").and_then(|o| s(o, "sentence")),
                restart_note: s(d, "restart_note"),
                latest_error: s(d, "latest_error"),
            });
        let external = v.get("external").filter(|e| e.is_object());
        Some(AppRow {
            name: s(v, "name").unwrap_or_else(|| id.clone()),
            kind: s(v, "kind").unwrap_or_else(|| "web".into()),
            description: s(v, "description").unwrap_or_default(),
            package: s(v, "package").unwrap_or_default(),
            installed: b(v, "installed"),
            version: s(v, "version"),
            latest_version: s(v, "latest_version"),
            update_available: b(v, "update_available"),
            update_label: s(v, "update_label"),
            update_tip: s(v, "update_tip"),
            running: b(v, "running"),
            status: s(v, "status").unwrap_or_else(|| "unknown".into()),
            source: s(v, "source"),
            external_port: if s(v, "source").as_deref() == Some("external") {
                external.and_then(|e| u(e, "port"))
            } else {
                None
            },
            url: s(v, "url"),
            port: u(v, "port"),
            pid: u(v, "pid"),
            last_error: s(v, "last_error"),
            needs_node_install: b(v, "needs_node_install"),
            install_available: b(v, "install_available"),
            install_blocked_reason: s(v, "install_blocked_reason"),
            install_parts: strs(v, "install_parts"),
            actions: strs(v, "actions"),
            active_job: v.get("active_job").and_then(AppJob::from_value),
            log_path: s(v, "log_path"),
            app_path: s(v, "app_path"),
            entities_count: v
                .get("content_summary")
                .and_then(|c| c.get("entities_count"))
                .and_then(Value::as_u64),
            tui,
            desktop,
            id,
        })
    }

    pub fn is_desktop(&self) -> bool {
        self.kind == "desktop"
    }
    pub fn is_external(&self) -> bool {
        self.external_port.is_some()
    }
    fn has(&self, action: &str) -> bool {
        self.actions.iter().any(|a| a == action)
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct NodeStatus {
    pub available: bool,
    pub version: Option<String>,
    /// system | managed | none
    pub source: String,
    pub path: Option<String>,
    pub install_available: bool,
    pub message: Option<String>,
    pub problems: Vec<String>,
    pub active_job: Option<AppJob>,
}

/// `GET /api/gateway/apps`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppsOverview {
    pub apps: Vec<AppRow>,
    pub node: NodeStatus,
    pub install_allowed: bool,
    /// None = not checked (latest=false); Some(false) = npm unreachable.
    pub registry_reachable: Option<bool>,
    pub registry_error: Option<String>,
    pub gateway_url: Option<String>,
    /// Where apps listen (127.0.0.1 = this machine only).
    pub apps_host: Option<String>,
    pub apps_dir: Option<String>,
    pub logs_dir: Option<String>,
    /// The prefix the gateway serves apps under (`/apps/`).
    pub apps_path_prefix: Option<String>,
    /// The gateway address a browser reaches (the web's own origin).
    pub browser_gateway_url: Option<String>,
}

impl AppsOverview {
    pub fn from_value(v: &Value) -> AppsOverview {
        let node = v
            .get("runtime")
            .and_then(|r| r.get("node"))
            .cloned()
            .unwrap_or(Value::Null);
        let registry = v.get("registry").cloned().unwrap_or(Value::Null);
        let data = v.get("data").cloned().unwrap_or(Value::Null);
        AppsOverview {
            apps: v
                .get("apps")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(AppRow::from_value).collect())
                .unwrap_or_default(),
            node: NodeStatus {
                available: b(&node, "available"),
                version: s(&node, "version"),
                source: s(&node, "source").unwrap_or_else(|| "none".into()),
                path: s(&node, "path"),
                install_available: b(&node, "install_available"),
                message: s(&node, "message"),
                problems: strs(&node, "problems"),
                active_job: node.get("active_job").and_then(AppJob::from_value),
            },
            install_allowed: b(v, "install_allowed"),
            registry_reachable: registry.get("reachable").and_then(Value::as_bool),
            registry_error: s(&registry, "error"),
            gateway_url: s(v, "gateway_url"),
            apps_host: s(v, "apps_host"),
            apps_dir: s(&data, "apps_dir"),
            logs_dir: s(&data, "logs_dir"),
            apps_path_prefix: s(v, "apps_path_prefix"),
            browser_gateway_url: s(v, "browser_gateway_url"),
        }
    }

    /// Every job the gateway reports as active on this overview, keyed
    /// the way this console tracks them.
    pub fn active_jobs(&self) -> Vec<(String, AppJob)> {
        let mut out = Vec::new();
        for a in &self.apps {
            if let Some(j) = &a.active_job {
                out.push((app_key(&a.id), j.clone()));
            }
            if let Some(j) = a.tui.as_ref().and_then(|t| t.active_job.clone()) {
                out.push((tui_key(&a.id), j));
            }
        }
        if let Some(j) = &self.node.active_job {
            out.push((NODE_KEY.to_string(), j.clone()));
        }
        out
    }
}

/// `GET /apps/{id}/logs?tail=N`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppLog {
    pub app_id: String,
    pub path: Option<String>,
    pub lines: Vec<String>,
    /// How many lines were ASKED for (the head line's honesty depends on it).
    pub tail: u32,
}

impl AppLog {
    pub fn from_value(app_id: &str, tail: u32, v: &Value) -> AppLog {
        AppLog {
            app_id: app_id.to_string(),
            path: s(v, "path"),
            lines: strs(v, "lines"),
            tail,
        }
    }

    /// The web panel's head line: never a silent cut.
    pub fn head(&self) -> String {
        let n = self.lines.len();
        if n == 0 {
            "The log is empty".into()
        } else if (n as u32) < self.tail {
            format!("The whole log · {n} line{}", if n == 1 { "" } else { "s" })
        } else {
            format!("The last {n} lines")
        }
    }
    /// "Show more" is offered while the log may hold more and the route's
    /// ceiling is not reached.
    pub fn can_show_more(&self) -> bool {
        (self.lines.len() as u32) >= self.tail && self.tail < crate::api::apps::APP_LOG_MAX
    }
    /// At the ceiling: older lines are only in the file.
    pub fn capped(&self) -> bool {
        (self.lines.len() as u32) >= crate::api::apps::APP_LOG_MAX
    }
}

/// A minted one-time sign-in link (POST /open), absolute.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppOpenLink {
    pub app_id: String,
    pub name: String,
    /// gateway base + `open_url`: works ONCE, for `expires_in_s`.
    pub link: String,
    /// Where the app itself listens (the handover redirects there).
    pub app_url: Option<String>,
    pub expires_in_s: Option<u64>,
    /// The app was started first (it was installed but stopped).
    pub started: bool,
    /// The console reaches the gateway on a loopback address, so the link
    /// only works in a browser on the gateway machine (or through a tunnel).
    pub tunnel_hint: Option<String>,
}

impl AppOpenLink {
    pub fn from_value(
        base_url: &str,
        app_id: &str,
        name: &str,
        started: bool,
        v: &Value,
    ) -> Option<AppOpenLink> {
        let open_url = s(v, "open_url")?;
        let link = format!("{}{}", base_url.trim_end_matches('/'), open_url);
        let app_url = s(v, "app_url");
        Some(AppOpenLink {
            app_id: app_id.to_string(),
            name: name.to_string(),
            tunnel_hint: tunnel_hint(base_url, app_url.as_deref()),
            link,
            app_url,
            expires_in_s: u(v, "expires_in_s"),
            started,
        })
    }
}

/// The host and port of an http(s) URL (no parsing crate: scheme://host[:port]/...).
fn host_port(url: &str) -> Option<(String, u16)> {
    let (scheme, rest) = url.split_once("://")?;
    let authority = rest.split('/').next().unwrap_or("");
    let authority = authority.rsplit('@').next().unwrap_or(authority);
    let default = if scheme.eq_ignore_ascii_case("https") {
        443
    } else {
        80
    };
    if let Some(stripped) = authority.strip_prefix('[') {
        let (h, tail) = stripped.split_once(']')?;
        let port = tail
            .strip_prefix(':')
            .and_then(|p| p.parse().ok())
            .unwrap_or(default);
        return Some((h.to_string(), port));
    }
    match authority.rsplit_once(':') {
        Some((h, p)) => Some((h.to_string(), p.parse().ok()?)),
        None => Some((authority.to_string(), default)),
    }
}

fn is_loopback_host(h: &str) -> bool {
    let h = h.to_ascii_lowercase();
    h == "localhost" || h == "::1" || h.ends_with(".localhost") || h.starts_with("127.")
}

/// When the console talks to the gateway over loopback, the one-time link
/// names a loopback address: it opens only in a browser on the gateway
/// machine. From another computer (a headless server over SSH) both the
/// gateway port and the app's port must be forwarded — say exactly which.
pub fn tunnel_hint(base_url: &str, app_url: Option<&str>) -> Option<String> {
    let (gh, gp) = host_port(base_url)?;
    if !is_loopback_host(&gh) {
        return None;
    }
    let mut forwards = vec![format!("-L {gp}:127.0.0.1:{gp}")];
    if let Some((ah, ap)) = app_url.and_then(host_port) {
        if is_loopback_host(&ah) && ap != gp {
            forwards.push(format!("-L {ap}:127.0.0.1:{ap}"));
        }
    }
    Some(format!(
        "This link opens in a browser on the gateway machine. From another computer, forward the ports first: ssh {} <gateway host>",
        forwards.join(" ")
    ))
}

// ---------------------------------------------------------------------
// Status, verbs, notes — the web console's rules
// ---------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Ok,
    Info,
    Warn,
    Err,
    Muted,
}

/// The status pill (web `APP_STATUS`), "Installing" while a job runs.
pub fn status_label(row: &AppRow, job_active: bool) -> (String, Tone) {
    if job_active {
        return ("Installing".into(), Tone::Info);
    }
    let (l, t) = match row.status.as_str() {
        "not_installed" => ("Not installed", Tone::Muted),
        "stopped" => ("Installed", Tone::Muted),
        "starting" => ("Starting", Tone::Info),
        "running" => ("Running", Tone::Ok),
        "stopping" => ("Stopping", Tone::Info),
        "crashed" => ("Stopped unexpectedly", Tone::Err),
        "crash_loop" => ("Keeps crashing", Tone::Err),
        other => return (other.to_string(), Tone::Muted),
    };
    (l.to_string(), t)
}

/// Every action the Apps screen can take, each on its own key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AppVerb {
    /// Open a browser app (starting it first when it is stopped): a
    /// one-time signed-in link. `path` rides for the first-run landing.
    Open,
    /// Desktop app: open it on the gateway computer's screen (POST /launch).
    DesktopOpen,
    Install,
    Update,
    /// Start without opening (POST /launch).
    Start,
    Stop,
    Log,
    /// Cancel the app's running install/update job.
    Cancel,
    /// Open the terminal version in a new terminal window on the gateway machine.
    OpenTerminal,
    /// Install (or update) the terminal version alone.
    InstallTerminal,
    /// Cancel the terminal version's install job.
    CancelTerminal,
}

impl AppVerb {
    /// The key the Apps screen binds (footer and detail panel say it).
    pub fn key(self) -> &'static str {
        match self {
            AppVerb::Open | AppVerb::DesktopOpen => "o",
            AppVerb::Install => "i",
            AppVerb::Update => "u",
            AppVerb::Start => "s",
            AppVerb::Stop => "x",
            AppVerb::Log => "l",
            AppVerb::Cancel => "c",
            AppVerb::OpenTerminal => "t",
            AppVerb::InstallTerminal => "T",
            AppVerb::CancelTerminal => "C",
        }
    }
}

/// One verb as offered for one app: its label and whether it is
/// available now — with the reason when it is not (never a dead key).
#[derive(Clone, Debug, PartialEq)]
pub struct VerbState {
    pub verb: AppVerb,
    pub label: String,
    pub available: Result<(), String>,
    /// The first-run landing path for Open (Entity with 0 entities: "/#new").
    pub path: Option<String>,
}

impl VerbState {
    fn on(verb: AppVerb, label: impl Into<String>) -> VerbState {
        VerbState {
            verb,
            label: label.into(),
            available: Ok(()),
            path: None,
        }
    }
    fn off(verb: AppVerb, label: impl Into<String>, why: impl Into<String>) -> VerbState {
        VerbState {
            verb,
            label: label.into(),
            available: Err(why.into()),
            path: None,
        }
    }
}

/// Entity with exactly 0 entities opens on its creation form (web
/// `APP_FIRST_RUN`): an unknown count is never 0.
pub fn first_run_landing(row: &AppRow) -> Option<(&'static str, &'static str)> {
    if row.id == "entity" && row.entities_count == Some(0) {
        Some(("Create your first entity", "/#new"))
    } else {
        None
    }
}

/// The state's ONE primary action (the web card's action row), or None
/// when the web shows no button (a non-admin during a job).
pub fn primary_verb(row: &AppRow, job: Option<&AppJob>, admin: bool) -> Option<VerbState> {
    let active = job.map(AppJob::is_active).unwrap_or(false);
    if active {
        return if admin {
            Some(VerbState::on(AppVerb::Cancel, "Cancel"))
        } else {
            Some(VerbState::off(
                AppVerb::Cancel,
                "Cancel",
                "Only an admin can cancel an install",
            ))
        };
    }
    if !row.installed {
        if row.has("install") && admin {
            return Some(VerbState::on(AppVerb::Install, "Install"));
        }
        let why = row.install_blocked_reason.clone().unwrap_or_else(|| {
            if admin {
                "Installing is not available right now".into()
            } else {
                "Only an admin can install apps".into()
            }
        });
        return Some(VerbState::off(AppVerb::Install, "Install", why));
    }
    if let Some(desk) = row.desktop.as_ref().filter(|_| row.is_desktop()) {
        return Some(if desk.launch_available {
            VerbState::on(AppVerb::DesktopOpen, "Open")
        } else {
            VerbState::off(
                AppVerb::DesktopOpen,
                "Open",
                desk.launch_blocked_reason
                    .clone()
                    .unwrap_or_else(|| "Only an admin can start apps".into()),
            )
        });
    }
    let landing = first_run_landing(row);
    let label = landing.map(|(l, _)| l).unwrap_or("Open");
    // Running: anyone signed in may open. Stopped: Open starts it first,
    // which is an admin action.
    let mut v = if row.running || (row.has("launch") && admin) {
        VerbState::on(AppVerb::Open, label)
    } else {
        VerbState::off(
            AppVerb::Open,
            label,
            if admin {
                "Starting is not available right now"
            } else {
                "Only an admin can start apps"
            },
        )
    };
    v.path = landing.map(|(_, p)| p.to_string());
    Some(v)
}

/// The Update verb of an installed row (R10.6), labelled with the
/// gateway's `update_label`. A row started outside the gateway never
/// updates from here: when a newer version is published `u` says the
/// gateway's sentence (where to update it; the card says "Latest x.y.z"),
/// and there is no verb when there is none.
pub fn update_verb(row: &AppRow, active: bool, admin: bool) -> Option<VerbState> {
    if row.is_external() {
        row.latest_version
            .as_ref()
            .filter(|_| row.update_available)?;
        return Some(VerbState::off(
            AppVerb::Update,
            "Update",
            row.update_tip.clone().unwrap_or_default(),
        ));
    }
    let label = match (&row.update_label, &row.latest_version) {
        (Some(l), _) if row.update_available => l.clone(),
        (None, Some(v)) if row.update_available => format!("Update to {v}"),
        _ => "Update".to_string(),
    };
    Some(if !row.update_available {
        VerbState::off(
            AppVerb::Update,
            label,
            "No newer version is published (or it was not checked: r checks again)",
        )
    } else if active {
        VerbState::off(AppVerb::Update, label, "An install is running")
    } else if !row.has("update") {
        VerbState::off(
            AppVerb::Update,
            label,
            "Updating is not available right now (installs are off for this caller)",
        )
    } else if !admin {
        VerbState::off(AppVerb::Update, label, "Only an admin can update apps")
    } else {
        VerbState::on(AppVerb::Update, label)
    })
}

/// Everything else the web offers under "Technical details", with the
/// reason each one is unavailable. Verbs the web never shows for this
/// kind of row are left out (a browser-only app has no terminal verbs;
/// a desktop app has no stop/start/log — its Update is `u`, R10.6).
pub fn secondary_verbs(
    row: &AppRow,
    job: Option<&AppJob>,
    tui_job: Option<&AppJob>,
    admin: bool,
) -> Vec<VerbState> {
    let mut out = Vec::new();
    let active = job.map(AppJob::is_active).unwrap_or(false);
    let not_admin = |what: &str| format!("Only an admin can {what}");
    // A browser app that is not installed shows no secondary verbs (the
    // web card has only Install then); a desktop app never has them.
    if !row.is_desktop() && row.installed {
        if let Some(port) = row.external_port {
            let why = format!(
                "Started outside the gateway on port {port}: stop, start, update and its log belong to whatever started it"
            );
            out.push(VerbState::off(AppVerb::Stop, "Stop", why));
            out.extend(update_verb(row, active, admin));
        } else {
            out.push(if !row.running {
                VerbState::off(
                    AppVerb::Stop,
                    "Stop",
                    format!("{} is not running", row.name),
                )
            } else if !row.has("stop") {
                VerbState::off(AppVerb::Stop, "Stop", "Stopping is not available right now")
            } else if !admin {
                VerbState::off(AppVerb::Stop, "Stop", not_admin("stop apps"))
            } else {
                VerbState::on(AppVerb::Stop, "Stop")
            });
            out.push(if !row.installed {
                VerbState::off(
                    AppVerb::Start,
                    "Start",
                    format!("{} is not installed", row.name),
                )
            } else if row.running {
                VerbState::off(
                    AppVerb::Start,
                    "Start",
                    format!("{} is already running", row.name),
                )
            } else if active {
                VerbState::off(AppVerb::Start, "Start", "An install is running")
            } else if !row.has("launch") {
                VerbState::off(
                    AppVerb::Start,
                    "Start",
                    "Starting is not available right now",
                )
            } else if !admin {
                VerbState::off(AppVerb::Start, "Start", not_admin("start apps"))
            } else {
                VerbState::on(AppVerb::Start, "Start (without opening)")
            });
            out.push(if !row.has("logs") {
                VerbState::off(
                    AppVerb::Log,
                    "Show log",
                    if row.installed {
                        "No log for this app".to_string()
                    } else {
                        format!("{} is not installed", row.name)
                    },
                )
            } else if !admin {
                VerbState::off(AppVerb::Log, "Show log", not_admin("read app logs"))
            } else {
                VerbState::on(AppVerb::Log, "Show log")
            });
            out.extend(update_verb(row, active, admin));
        }
    }
    // R10.6: the Assistant updates like a browser app (`u`).
    if row.is_desktop() && row.installed {
        out.extend(update_verb(row, active, admin));
    }
    if let Some(t) = &row.tui {
        let t_active = tui_job.map(AppJob::is_active).unwrap_or(false);
        if t_active {
            out.push(if admin {
                VerbState::on(AppVerb::CancelTerminal, "Cancel terminal install")
            } else {
                VerbState::off(
                    AppVerb::CancelTerminal,
                    "Cancel terminal install",
                    not_admin("cancel an install"),
                )
            });
        }
        out.push(if !t.installed {
            VerbState::off(
                AppVerb::OpenTerminal,
                "Open in Terminal",
                "The terminal version is not installed",
            )
        } else if !row.installed {
            VerbState::off(
                AppVerb::OpenTerminal,
                "Open in Terminal",
                format!("{} is not installed", row.name),
            )
        } else if !t.launch_available {
            VerbState::off(
                AppVerb::OpenTerminal,
                "Open in Terminal",
                format!(
                    "{} — y copies the command to run where it is installed",
                    t.launch_blocked_reason
                        .clone()
                        .unwrap_or_else(|| "Not available from here".into())
                ),
            )
        } else if t_active {
            VerbState::off(
                AppVerb::OpenTerminal,
                "Open in Terminal",
                "Its install is running",
            )
        } else {
            VerbState::on(AppVerb::OpenTerminal, "Open in Terminal")
        });
        let label = match (&t.latest_version, t.installed && t.update_available) {
            (Some(v), true) => format!("Update terminal app to {v}"),
            (None, true) => "Update terminal app".to_string(),
            _ if t.installed => "Update terminal app".to_string(),
            _ => "Install terminal app".to_string(),
        };
        out.push(if t_active {
            VerbState::off(AppVerb::InstallTerminal, label, "Its install is running")
        } else if t.installed && !t.update_available {
            VerbState::off(
                AppVerb::InstallTerminal,
                label,
                match &t.version {
                    Some(v) => format!("The terminal app is up to date ({v})"),
                    None => "The terminal app is up to date".to_string(),
                },
            )
        } else if !t.install_available {
            let mut why = t
                .install_blocked_reason
                .clone()
                .unwrap_or_else(|| "Installing is not available right now".into());
            if t.install_command.is_some() {
                why.push_str(" — y copies the install command");
            }
            VerbState::off(AppVerb::InstallTerminal, label, why)
        } else if !t.installed && !row.installed {
            VerbState::off(
                AppVerb::InstallTerminal,
                label,
                format!("Install {} first: its Install installs both", row.name),
            )
        } else if !admin {
            VerbState::off(AppVerb::InstallTerminal, label, not_admin("install apps"))
        } else {
            VerbState::on(AppVerb::InstallTerminal, label)
        });
    }
    out
}

/// A transient or persistent result on an app (web `appNotify`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AppNote {
    pub tone: Option<Tone>,
    pub text: String,
    pub hint: Option<String>,
    /// (label, command) lines to copy.
    pub commands: Vec<(String, String)>,
    pub details: Option<String>,
}

impl AppNote {
    pub fn ok(text: impl Into<String>) -> AppNote {
        AppNote {
            tone: Some(Tone::Ok),
            text: text.into(),
            ..AppNote::default()
        }
    }
    pub fn info(text: impl Into<String>) -> AppNote {
        AppNote {
            tone: Some(Tone::Info),
            text: text.into(),
            ..AppNote::default()
        }
    }
    pub fn is_err(&self) -> bool {
        self.tone == Some(Tone::Err)
    }
}

/// The gateway's own words for an apps refusal: the route body's
/// `message`, `hint`, `details` and any command to copy (`command`,
/// `signin_command`), prefixed with what was being attempted.
pub fn app_error_note(attempt: &str, e: &ApiError) -> AppNote {
    let body = e.body.as_ref();
    let msg = body
        .and_then(|b| b.get("message"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| e.to_string());
    let status = e
        .status()
        .map(|c| format!(" (HTTP {c})"))
        .unwrap_or_default();
    let mut commands = Vec::new();
    if let Some(c) = body.and_then(|b| s(b, "command")) {
        commands.push(("Command".to_string(), c));
    }
    if let Some(c) = body.and_then(|b| s(b, "signin_command")) {
        commands.push(("First time there, sign in once".to_string(), c));
    }
    AppNote {
        tone: Some(Tone::Err),
        text: format!("Could not {attempt}: {msg}{status}"),
        hint: body.and_then(|b| s(b, "hint")),
        commands,
        details: body.and_then(|b| s(b, "details")),
    }
}

/// What a FINISHED job leaves on its app (web `appJobResult`).
pub fn job_result_note(key: &str, name: &str, job: &AppJob) -> Option<AppNote> {
    let ver = job
        .result_version
        .clone()
        .map(|v| format!(" {v}"))
        .unwrap_or_default();
    match job.state.as_str() {
        "succeeded" if key.starts_with("tui:") => Some(AppNote::ok(format!(
            "{name}'s terminal app{ver} is installed."
        ))),
        "succeeded" if key == NODE_KEY => Some(AppNote::ok(format!("Node.js{ver} is installed."))),
        "succeeded" => {
            let verb = if job.kind == "update" {
                "updated"
            } else {
                "installed"
            };
            Some(AppNote::ok(if job.result_url.is_some() {
                format!("{name}{ver} is {verb} and running.")
            } else if job.result_terminal {
                format!("{name}{ver} is {verb}, for the browser and the terminal.")
            } else {
                format!("{name}{ver} is {verb}.")
            }))
        }
        "failed" => {
            let err = job.error.clone().unwrap_or_default();
            let fallback = if key.starts_with("tui:") {
                format!("{name}'s terminal app did not install.")
            } else if key == NODE_KEY {
                "Node.js did not install.".to_string()
            } else {
                "The install did not finish.".to_string()
            };
            let text = if err.message.is_empty() {
                fallback
            } else {
                err.message
            };
            let log = job.log_text();
            Some(AppNote {
                tone: Some(Tone::Err),
                text,
                hint: err.hint,
                commands: Vec::new(),
                details: (!log.is_empty()).then_some(log),
            })
        }
        "cancelled" => Some(AppNote::info(format!(
            "{name}: cancelled. Nothing was changed."
        ))),
        _ => None,
    }
}

/// Everything copyable on an app row (the web's Copy buttons): the link
/// target, the npx line, the terminal commands, the desktop commands and
/// any command a refusal carried.
pub fn copyables(row: &AppRow, note: Option<&AppNote>) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    if let Some(u) = &row.url {
        out.push(("Address".into(), u.clone()));
    }
    if let Some(t) = &row.tui {
        if let Some(c) = &t.command {
            out.push(("Terminal command".into(), c.clone()));
        }
        if let Some(c) = &t.signin_command {
            out.push(("Terminal sign-in (first time)".into(), c.clone()));
        }
        if !t.installed {
            if let Some(c) = &t.install_command {
                out.push(("Terminal install command".into(), c.clone()));
            }
        }
    }
    if let Some(d) = &row.desktop {
        if let Some(c) = &d.launch_command {
            out.push(("Launch command".into(), c.clone()));
        }
        if !row.installed {
            if let Some(c) = &d.install_command {
                out.push(("Install command".into(), c.clone()));
            }
        }
    }
    if !row.is_desktop() && !row.package.is_empty() {
        out.push(("npm".into(), format!("npx {}", row.package)));
    }
    if let Some(n) = note {
        for (l, c) in &n.commands {
            if !out.iter().any(|(_, x)| x == c) {
                out.push((l.clone(), c.clone()));
            }
        }
    }
    out
}

// ---------------------------------------------------------------------
// The reactive state
// ---------------------------------------------------------------------

/// The Apps screen's signals (one field on `Store`).
#[derive(Clone, Copy)]
pub struct AppsStore {
    pub overview: Signal<Loadable<AppsOverview>>,
    /// The latest known state of every job this console watches, by key
    /// (`app:<id>`, `tui:<id>`, `__node__`). A finished job stays so its
    /// failure keeps showing (web: the card's failure box).
    pub jobs: Signal<Vec<(String, AppJob)>>,
    /// The last result per key (success notes and refusals).
    pub notes: Signal<Vec<(String, AppNote)>>,
    /// Keys with a request in flight (their verbs say so, and refuse twice).
    pub pending: Signal<Vec<String>>,
    /// The open log panel's content.
    pub log: Signal<Loadable<AppLog>>,
    /// A freshly minted sign-in link: the screen shows it in a modal.
    pub open_link: Signal<Option<AppOpenLink>>,
    /// Job poll chain: generation gate + "a chain is live".
    pub poll_gen: Signal<u64>,
    pub polling: Signal<bool>,
    pub sel: Signal<usize>,
}

impl AppsStore {
    pub fn create(cx: Scope) -> AppsStore {
        AppsStore {
            overview: cx.signal(Loadable::default()),
            jobs: cx.signal(Vec::new()),
            notes: cx.signal(Vec::new()),
            pending: cx.signal(Vec::new()),
            log: cx.signal(Loadable::default()),
            open_link: cx.signal(None),
            poll_gen: cx.signal(0),
            polling: cx.signal(false),
            sel: cx.signal(0),
        }
    }

    /// New gateway / principal: nothing of the old one survives, and a
    /// live poll chain dies on the generation bump.
    pub fn reset(&self) {
        self.overview.set(Loadable::NotAsked);
        self.jobs.set(Vec::new());
        self.notes.set(Vec::new());
        self.pending.set(Vec::new());
        self.log.set(Loadable::NotAsked);
        self.open_link.set(None);
        self.poll_gen.update(|g| *g += 1);
        self.polling.set(false);
    }

    /// The job to show for `key`: the one this console tracks, else the
    /// one the overview reports.
    pub fn job_for(&self, key: &str, fallback: Option<&AppJob>) -> Option<AppJob> {
        self.jobs
            .with(|j| j.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone()))
            .or_else(|| fallback.cloned())
    }

    pub fn note_for(&self, key: &str) -> Option<AppNote> {
        self.notes
            .with(|n| n.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone()))
    }

    pub fn set_job(&self, key: &str, job: AppJob) {
        let key = key.to_string();
        self.jobs.update(move |j| {
            j.retain(|(k, _)| *k != key);
            j.push((key, job));
        });
    }

    pub fn set_note(&self, key: &str, note: Option<AppNote>) {
        let key = key.to_string();
        self.notes.update(move |n| {
            n.retain(|(k, _)| *k != key);
            if let Some(note) = note {
                n.push((key, note));
            }
        });
    }

    pub fn is_pending(&self, key: &str) -> bool {
        self.pending.with(|p| p.iter().any(|k| k == key))
    }

    pub fn set_pending(&self, key: &str, on: bool) {
        let key = key.to_string();
        self.pending.update(move |p| {
            p.retain(|k| *k != key);
            if on {
                p.push(key);
            }
        });
    }

    /// (key, job id) of every tracked job still queued/running.
    pub fn active_job_ids(&self) -> Vec<(String, String)> {
        self.jobs.with_untracked(|j| {
            j.iter()
                .filter(|(_, job)| job.is_active())
                .map(|(k, job)| (k.clone(), job.id.clone()))
                .collect()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(v: Value) -> AppRow {
        AppRow::from_value(&v).expect("row parses")
    }

    fn not_installed() -> AppRow {
        row(json!({
            "id": "observer", "name": "Observer", "kind": "web",
            "package": "@abstractframework/observer",
            "installed": false, "running": false, "status": "not_installed",
            "actions": ["install"], "install_parts": ["web"],
            "interfaces": [{"kind": "web"}]
        }))
    }

    fn running_managed() -> AppRow {
        row(json!({
            "id": "flow", "name": "Flow Editor", "kind": "web",
            "package": "@abstractframework/flow",
            "installed": true, "version": "0.3.20", "latest_version": "0.3.21",
            "update_available": true, "running": true, "status": "running",
            "source": "gateway", "url": "http://127.0.0.1:3003/", "port": 3003,
            "actions": ["open", "stop", "update", "logs"]
        }))
    }

    #[test]
    fn primary_follows_the_web_card_for_every_state() {
        let r = not_installed();
        assert_eq!(
            primary_verb(&r, None, true).unwrap(),
            VerbState::on(AppVerb::Install, "Install")
        );
        let off = primary_verb(&r, None, false).unwrap();
        assert_eq!(off.available, Err("Only an admin can install apps".into()));

        let mut blocked = not_installed();
        blocked.actions.clear();
        blocked.install_blocked_reason = Some("Installing apps is off on this gateway.".into());
        assert_eq!(
            primary_verb(&blocked, None, true).unwrap().available,
            Err("Installing apps is off on this gateway.".into())
        );

        let job = AppJob {
            id: "j1".into(),
            state: "running".into(),
            ..AppJob::default()
        };
        assert_eq!(
            primary_verb(&r, Some(&job), true).unwrap().verb,
            AppVerb::Cancel
        );
        assert!(primary_verb(&r, Some(&job), false)
            .unwrap()
            .available
            .is_err());

        let run = running_managed();
        assert_eq!(
            primary_verb(&run, None, false).unwrap(),
            VerbState::on(AppVerb::Open, "Open")
        );

        let mut stopped = running_managed();
        stopped.running = false;
        stopped.status = "crashed".into();
        stopped.actions = vec!["launch".into(), "logs".into()];
        assert_eq!(
            primary_verb(&stopped, None, true).unwrap().available,
            Ok(())
        );
        assert_eq!(
            primary_verb(&stopped, None, false).unwrap().available,
            Err("Only an admin can start apps".into())
        );
        assert_eq!(status_label(&stopped, false).0, "Stopped unexpectedly");
    }

    #[test]
    fn entity_with_zero_entities_opens_on_its_creation_form() {
        let mut e = running_managed();
        e.id = "entity".into();
        e.entities_count = Some(0);
        let v = primary_verb(&e, None, true).unwrap();
        assert_eq!(v.label, "Create your first entity");
        assert_eq!(v.path.as_deref(), Some("/#new"));
        e.entities_count = None; // unknown is never 0
        assert_eq!(primary_verb(&e, None, true).unwrap().path, None);
    }

    #[test]
    fn external_apps_offer_open_only_and_say_why() {
        let ext = row(json!({
            "id": "observer", "name": "Observer", "installed": true, "running": true,
            "status": "running", "source": "external",
            "external": {"port": 3001, "detail": "Started outside the gateway on port 3001"},
            "actions": ["open"]
        }));
        assert!(ext.is_external());
        assert_eq!(primary_verb(&ext, None, true).unwrap().verb, AppVerb::Open);
        let sec = secondary_verbs(&ext, None, None, true);
        assert_eq!(sec.len(), 1, "{sec:?}");
        assert_eq!(sec[0].verb, AppVerb::Stop);
        assert!(sec[0].available.as_ref().unwrap_err().contains("port 3001"));
    }

    #[test]
    fn secondary_verbs_gate_on_admin_with_reasons() {
        let run = running_managed();
        let admin = secondary_verbs(&run, None, None, true);
        let find = |v: &[VerbState], verb| v.iter().find(|x| x.verb == verb).cloned().unwrap();
        assert_eq!(find(&admin, AppVerb::Stop).available, Ok(()));
        assert_eq!(find(&admin, AppVerb::Update).label, "Update to 0.3.21");
        assert_eq!(find(&admin, AppVerb::Update).available, Ok(()));
        assert_eq!(find(&admin, AppVerb::Log).available, Ok(()));
        assert!(find(&admin, AppVerb::Start).available.is_err());
        let user = secondary_verbs(&run, None, None, false);
        assert_eq!(
            find(&user, AppVerb::Stop).available,
            Err("Only an admin can stop apps".into())
        );
        assert_eq!(
            find(&user, AppVerb::Log).available,
            Err("Only an admin can read app logs".into())
        );
        assert!(
            !admin.iter().any(|v| v.verb == AppVerb::OpenTerminal),
            "browser-only app"
        );
    }

    #[test]
    fn terminal_verbs_mirror_app_tui_parts() {
        let code = row(json!({
            "id": "code", "name": "Code", "installed": true, "running": true, "status": "running",
            "source": "gateway", "actions": ["open", "stop", "logs"],
            "interfaces": [{"kind": "web"}, {
                "kind": "tui", "installed": true, "version": "0.6.0", "install_available": false,
                "install_method": "release_binary", "launch_available": false,
                "launch_blocked_reason": "This browser is on another computer.",
                "launch_mode": "copy", "command": "abstractcode --gateway http://gw:8080",
                "signin_command": "abstractcode login --gateway http://gw:8080 --token <your token>",
                "install_command": "cargo install abstractcode"
            }]
        }));
        let sec = secondary_verbs(&code, None, None, true);
        let open = sec
            .iter()
            .find(|v| v.verb == AppVerb::OpenTerminal)
            .unwrap();
        assert!(open
            .available
            .as_ref()
            .unwrap_err()
            .contains("another computer"));
        let cp = copyables(&code, None);
        assert!(
            cp.iter().any(|(_, c)| c.starts_with("abstractcode login")),
            "{cp:?}"
        );
        assert!(
            !cp.iter().any(|(_, c)| c == "cargo install abstractcode"),
            "installed: no install line"
        );
        let tjob = AppJob {
            id: "t".into(),
            state: "queued".into(),
            ..AppJob::default()
        };
        let sec = secondary_verbs(&code, None, Some(&tjob), true);
        assert!(sec
            .iter()
            .any(|v| v.verb == AppVerb::CancelTerminal && v.available.is_ok()));
    }

    #[test]
    fn desktop_app_opens_on_the_gateway_screen_or_says_why_not() {
        let desk = row(json!({
            "id": "assistant", "name": "Assistant", "kind": "desktop", "installed": true,
            "running": false, "status": "stopped", "actions": ["open"],
            "desktop": {"launch_available": false, "launch_blocked": "other_computer",
                        "launch_blocked_reason": "The Assistant runs on the gateway's computer: open it there."}
        }));
        let p = primary_verb(&desk, None, true).unwrap();
        assert_eq!(p.verb, AppVerb::DesktopOpen);
        assert!(p.available.unwrap_err().contains("open it there"));
        // R10.6: the desktop app's one secondary verb is Update (off: up to date).
        let sec = secondary_verbs(&desk, None, None, true);
        assert_eq!(sec.len(), 1, "{sec:?}");
        assert_eq!(sec[0].verb, AppVerb::Update);
        assert!(sec[0].available.is_err());
    }

    #[test]
    fn job_parse_progress_and_result_notes() {
        let j = AppJob::from_value(&json!({
            "id": "abc", "kind": "install", "app_id": "code", "title": "Installing Code",
            "state": "running", "percent": 42.4, "indeterminate": false,
            "bytes_done": 1048576, "bytes_total": 3145728, "message": "Downloading",
            "parts": [{"id": "install", "label": "Code in the browser", "state": "done"},
                      {"id": "install-tui", "label": "Code in the terminal", "state": "running"}],
            "result": {}, "error": null, "log_tail": ["a", "b"]
        }))
        .unwrap();
        assert!(j.is_active());
        assert_eq!(
            j.progress_line("x"),
            "Installing Code · 42% · 1.0 MB / 3.0 MB · Downloading"
        );
        assert_eq!(part_word(&j.parts[1].state), "Installing...");
        let done = AppJob {
            state: "succeeded".into(),
            result_version: Some("0.5.0".into()),
            result_terminal: true,
            ..j.clone()
        };
        assert_eq!(
            job_result_note("app:code", "Code", &done).unwrap().text,
            "Code 0.5.0 is installed, for the browser and the terminal."
        );
        let failed = AppJob::from_value(&json!({
            "id": "abc", "state": "failed",
            "error": {"reason": "network_unavailable", "message": "npm is not reachable", "hint": "Check the internet"},
            "details": "full log"
        }))
        .unwrap();
        let n = job_result_note("app:code", "Code", &failed).unwrap();
        assert!(n.is_err());
        assert_eq!(n.hint.as_deref(), Some("Check the internet"));
        assert_eq!(n.details.as_deref(), Some("full log"));
        let c = AppJob {
            state: "cancelled".into(),
            ..j
        };
        assert_eq!(
            job_result_note(NODE_KEY, "Node.js", &c).unwrap().text,
            "Node.js: cancelled. Nothing was changed."
        );
    }

    #[test]
    fn refusal_note_keeps_the_gateways_words_and_command() {
        let e = ApiError {
            kind: crate::api::ApiErrorKind::Http(409),
            message: "raw".into(),
            body: Some(json!({
                "ok": false, "reason": "not_on_gateway_machine",
                "message": "Code can only open in a terminal on the gateway's own screen.",
                "hint": "Copy the command.", "command": "abstractcode --gateway http://gw:8080",
                "signin_command": "abstractcode login --gateway http://gw:8080 --token <your token>"
            })),
            timed_out: false,
        };
        let n = app_error_note("open Code for the terminal", &e);
        assert_eq!(
            n.text,
            "Could not open Code for the terminal: Code can only open in a terminal on the gateway's own screen. (HTTP 409)"
        );
        assert_eq!(n.commands.len(), 2);
        assert_eq!(n.hint.as_deref(), Some("Copy the command."));
    }

    #[test]
    fn log_head_is_never_a_silent_cut() {
        let mut l = AppLog {
            app_id: "flow".into(),
            path: None,
            lines: vec!["x".into(); 37],
            tail: 200,
        };
        assert_eq!(l.head(), "The whole log · 37 lines");
        assert!(!l.can_show_more());
        l.lines = vec!["x".into(); 200];
        assert_eq!(l.head(), "The last 200 lines");
        assert!(l.can_show_more());
        l.tail = 5000;
        l.lines = vec!["x".into(); 5000];
        assert!(!l.can_show_more() && l.capped());
    }

    #[test]
    fn open_link_is_absolute_and_tunnel_hint_names_both_ports() {
        let l = AppOpenLink::from_value(
            "http://127.0.0.1:8080",
            "flow",
            "Flow Editor",
            false,
            &json!({"open_url": "/apps/handover/abc", "app_url": "http://127.0.0.1:3003/", "expires_in_s": 120}),
        )
        .unwrap();
        assert_eq!(l.link, "http://127.0.0.1:8080/apps/handover/abc");
        let hint = l.tunnel_hint.unwrap();
        assert!(
            hint.contains("-L 8080:127.0.0.1:8080 -L 3003:127.0.0.1:3003"),
            "{hint}"
        );
        assert_eq!(
            tunnel_hint("http://gw.lan:8080", Some("http://gw.lan:3003/")),
            None
        );
    }

    #[test]
    fn overview_collects_active_jobs_under_console_keys() {
        let o = AppsOverview::from_value(&json!({
            "runtime": {"node": {"available": false, "install_available": true,
                                  "active_job": {"id": "n1", "state": "running"}}},
            "apps": [{"id": "code", "active_job": {"id": "a1", "state": "queued"},
                      "interfaces": [{"kind": "tui", "active_job": {"id": "t1", "state": "running"}}]}],
            "registry": {"reachable": false, "error": "offline"}
        }));
        let keys: Vec<String> = o.active_jobs().into_iter().map(|(k, _)| k).collect();
        assert_eq!(keys, vec!["app:code", "tui:code", "__node__"]);
        assert_eq!(o.registry_reachable, Some(false));
        assert!(!o.node.available && o.node.install_available);
    }
}
