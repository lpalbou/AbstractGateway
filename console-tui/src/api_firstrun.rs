//! Headless first-run parity with the web console's setup guide.
//!
//! The web guide (`console.py`, `FIRST_RUN_STEPS` = welcome → engines →
//! model → apps → done) reads and records the first-run state through
//! `GET/POST /api/gateway/host/first-run`, summarises the computer from
//! `GET /api/gateway/host/state` on its welcome step, and on its model
//! step shows the recommended plan of `GET /api/gateway/models/availability`
//! (`recommended.recommended`, each row with AbstractCore's fit `warning`)
//! plus "Download all" (`POST /api/gateway/models/download
//! {"recommended": true}` → one parent job `grp_…`).
//!
//! This module holds the client calls for those routes and the pure
//! folds of their payloads; the screens (`ui/welcome.rs`, the Routes
//! plan panel, the Review finish row) and the worker arms consume them.

use serde_json::{json, Value};

use super::{ApiResult, GatewayClient};

impl GatewayClient {
    /// `GET /host/first-run` — any authenticated principal.
    pub fn first_run_state(&self) -> ApiResult<Value> {
        self.get("/host/first-run", false)
    }

    /// `POST /host/first-run {"outcome": "finished"|"skipped"}` — admin
    /// only; the same body the web guide's Finish / Skip setup send.
    pub fn complete_first_run(&self, outcome: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            "/host/first-run",
            &json!({ "outcome": outcome }),
            false,
        )
    }

    /// `POST /models/download {"recommended": true}` — the web guide's
    /// "Download all": one parent job (`group`) over one child per model
    /// (`jobs`). `dry_run` exists for the live proof only (the gateway
    /// resolves every child without fetching); the console sends false.
    pub fn download_recommended(&self, dry_run: bool) -> ApiResult<Value> {
        let mut body = json!({ "recommended": true });
        if dry_run {
            body["dry_run"] = Value::Bool(true);
        }
        // Slow agent: the gateway resolves each child's provider tool
        // before it answers (the web console sends this one `slow` too).
        self.send("POST", "/models/download", &body, true)
    }

    // `cancel_model_download` (POST /models/download/{job}/cancel
    // {"via": "console"}) lives in api_engines.rs: one client method for
    // the Routes "Download all" cancel and the shared screens' cancel.
}

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|x| !x.is_empty())
}

/// `gateway_first_run_v1`: whether the guide ran for this data dir.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FirstRunState {
    pub completed: bool,
    pub completed_at: Option<String>,
    pub completed_by: Option<String>,
    /// "finished" | "skipped" once completed.
    pub outcome: Option<String>,
}

impl FirstRunState {
    pub fn from_value(v: &Value) -> FirstRunState {
        FirstRunState {
            completed: v.get("completed").and_then(Value::as_bool).unwrap_or(false),
            completed_at: s(v, "completed_at"),
            completed_by: s(v, "completed_by"),
            outcome: s(v, "outcome"),
        }
    }

    /// One line for the welcome and finish surfaces.
    pub fn line(&self) -> String {
        if !self.completed {
            return "not completed — the guide opens by itself until Finish or Skip setup".into();
        }
        let mut out = format!(
            "completed ({})",
            self.outcome.as_deref().unwrap_or("outcome not reported")
        );
        if let Some(at) = &self.completed_at {
            out.push_str(&format!(" at {at}"));
        }
        if let Some(by) = &self.completed_by {
            out.push_str(&format!(" by {by}"));
        }
        out
    }
}

/// The boot decision the web console makes in `firstRunShouldAutoOpen`
/// (+ `maybeOpenFirstRun`'s admin check): the guide opens by itself only
/// for an ADMIN (its writes are admin routes) and only while first run
/// is NOT completed. `true` = start (stay) in the wizard.
pub fn first_run_auto_wizard(state: &FirstRunState, admin: bool) -> bool {
    admin && !state.completed
}

/// The verify-after-write rule for Finish / Skip: the follow-up GET must
/// show the first run completed WITH the outcome that was asked for.
pub fn first_run_verify(requested: &str, after: &FirstRunState) -> Result<String, String> {
    if !after.completed {
        return Err("GET /host/first-run still reports completed=false".into());
    }
    match after.outcome.as_deref() {
        Some(o) if o == requested => Ok(format!("GET /host/first-run: {}", after.line())),
        other => Err(format!(
            "GET /host/first-run reports outcome {:?}, not {requested:?}",
            other.unwrap_or("none")
        )),
    }
}

/// `gateway.service.platform` → the web's OS words
/// (`firstRunOsLabel`); any other value shows nothing, never a guess.
fn os_label(platform: &str) -> Option<String> {
    match platform.trim().to_ascii_lowercase().as_str() {
        "darwin" => Some("macOS".into()),
        "windows" => Some("Windows".into()),
        "linux" => Some("Linux".into()),
        _ => None,
    }
}

/// The welcome step's six facts (`loadFirstRunWelcome`), folded from
/// `GET /host/state`. Absent facts stay `None` — rendered as "unknown",
/// never guessed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WelcomeSummary {
    pub computer: Option<String>,
    pub os: Option<String>,
    pub ram_total_bytes: Option<u64>,
    pub gpus: Vec<String>,
    pub data_dir: Option<String>,
    pub data_dir_source: Option<String>,
    pub auth_mode: Option<String>,
    pub service_installed: Option<bool>,
    pub service_mechanism: Option<String>,
    pub console_url: Option<String>,
    /// The same `first_run` block `/host/state` carries in `gateway`.
    pub first_run: Option<FirstRunState>,
}

impl WelcomeSummary {
    pub fn from_host_state(v: &Value) -> WelcomeSummary {
        let host = v.get("host").cloned().unwrap_or(Value::Null);
        let gw = v.get("gateway").cloned().unwrap_or(Value::Null);
        let svc = gw.get("service").cloned().unwrap_or(Value::Null);
        let gpus = v
            .get("gpu")
            .and_then(|g| g.get("gpus"))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|g| s(g, "name")).collect())
            .unwrap_or_default();
        WelcomeSummary {
            // AbstractCore's host identity names the machine `host_name`
            // (the web's Computer tile reads exactly that).
            computer: s(&host, "host_name"),
            // The one OS fact in /host/state is the gateway's service
            // block (web `firstRunOsLabel(svc.platform)`); `host` has none.
            os: s(&svc, "platform").and_then(|p| os_label(&p)),
            ram_total_bytes: v
                .get("memory")
                .and_then(|m| m.get("ram"))
                .and_then(|r| r.get("total_bytes"))
                .and_then(Value::as_u64),
            gpus,
            data_dir: s(&gw, "data_dir"),
            data_dir_source: s(&gw, "data_dir_source"),
            auth_mode: s(&gw, "auth_mode"),
            service_installed: svc.get("installed").and_then(Value::as_bool),
            service_mechanism: s(&svc, "mechanism"),
            console_url: s(&gw, "console_url"),
            first_run: gw
                .get("first_run")
                .filter(|f| f.is_object())
                .map(FirstRunState::from_value),
        }
    }

    /// (label, value, note) rows in the web tiles' order and words.
    pub fn rows(&self) -> Vec<(&'static str, String, String)> {
        let gpus = if self.gpus.is_empty() {
            "None detected".to_string()
        } else {
            self.gpus.join(", ")
        };
        let installed = self.service_installed == Some(true);
        vec![
            (
                "Computer",
                self.computer
                    .clone()
                    .unwrap_or_else(|| "This computer".into()),
                self.os.clone().unwrap_or_default(),
            ),
            (
                "Memory",
                self.ram_total_bytes
                    .map(fmt_bytes)
                    .unwrap_or_else(|| "Unknown".into()),
                "Available to models and apps".into(),
            ),
            (
                "Graphics",
                gpus,
                if self.gpus.is_empty() {
                    "Models run on the processor".into()
                } else {
                    "Used to run local models".into()
                },
            ),
            (
                "Data folder",
                self.data_dir.clone().unwrap_or_else(|| "?".into()),
                format!(
                    "Runs, workflows and settings live here ({})",
                    self.data_dir_source.as_deref().unwrap_or("?")
                ),
            ),
            (
                "Sign-in",
                match self.auth_mode.as_deref() {
                    Some("users") => "User accounts".into(),
                    Some(m) => m.to_string(),
                    None => "Unknown".into(),
                },
                if self.auth_mode.as_deref() == Some("users") {
                    "You are the admin".into()
                } else {
                    String::new()
                },
            ),
            (
                "Starts at login",
                match self.service_installed {
                    Some(true) => "Yes".into(),
                    Some(false) => "Not yet".into(),
                    None => "Unknown".into(),
                },
                if installed {
                    format!(
                        "Installed as a {}",
                        self.service_mechanism.as_deref().unwrap_or("service")
                    )
                } else {
                    "Keeps the gateway running after a restart: abstractgateway service install"
                        .into()
                },
            ),
        ]
    }
}

/// Binary units, one decimal — the web console's `_fmtBytes` shape.
pub fn fmt_bytes(n: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", UNITS[i])
    }
}

/// One card of the web guide's "Recommended for this computer"
/// (`availability.recommended.recommended[]`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PlanRow {
    pub route: String,
    pub provider: String,
    pub artifact: String,
    /// installed | absent | unknown | not_applicable
    pub status: String,
    /// "memory >= 128 GiB" — which memory tier chose it.
    pub tier: Option<String>,
    /// AbstractCore's fit estimate doubting the pick, verbatim.
    pub warning: Option<String>,
    pub evidence: Option<String>,
    pub instruction: Option<String>,
    /// The engine that runs it is not installed here (AbstractCore
    /// `engine_missing`) — the other half of "ready" besides the weights.
    pub engine_missing: Option<crate::store::EngineMissing>,
    /// AbstractCore's fit verdict for the pick (`fit_verdict`).
    pub fit_verdict: Option<String>,
    /// `needs_gpu_limit`: the exact sysctl that makes it fit (`gpu_limit`,
    /// on the row or under `fit`), when the payload carries it.
    pub gpu_limit: Option<GpuLimit>,
}

/// AbstractCore's `fit.gpu_limit` (utils/model_fit.py `_gpu_limit_for`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GpuLimit {
    pub command: String,
    pub required_mb: u64,
    pub needs_admin: bool,
    pub resets_at_restart: bool,
}

impl GpuLimit {
    pub fn from_value(v: &Value) -> Option<GpuLimit> {
        let command = s(v, "command").filter(|c| !c.trim().is_empty())?;
        Some(GpuLimit {
            command,
            required_mb: v.get("required_mb").and_then(Value::as_u64).unwrap_or(0),
            needs_admin: v
                .get("needs_admin")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            resets_at_restart: v
                .get("resets_at_restart")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        })
    }
}

impl PlanRow {
    /// `needs_gpu_limit`: "fits after raising the GPU memory limit: <command>
    /// (admin; resets at restart)"; None for every other verdict.
    pub fn gpu_limit_text(&self) -> Option<String> {
        if self.fit_verdict.as_deref() != Some("needs_gpu_limit") {
            return None;
        }
        Some(match &self.gpu_limit {
            Some(g) => {
                let cost: Vec<&str> = [
                    g.needs_admin.then_some("admin"),
                    g.resets_at_restart.then_some("resets at restart"),
                ]
                .into_iter()
                .flatten()
                .collect();
                if cost.is_empty() {
                    format!("fits after raising the GPU memory limit: {}", g.command)
                } else {
                    format!(
                        "fits after raising the GPU memory limit: {} ({})",
                        g.command,
                        cost.join("; ")
                    )
                }
            }
            None => "fits after raising the GPU memory limit".to_string(),
        })
    }

    pub fn title(&self) -> &'static str {
        route_title(&self.route)
    }

    /// The status in the web cards' words (`WEIGHT_LABELS`).
    pub fn status_label(&self) -> &str {
        match self.status.as_str() {
            "installed" => "Installed",
            "absent" => "Not downloaded",
            "not_applicable" => "Remote",
            "unknown" => "Unknown",
            "" => "unknown",
            other => other,
        }
    }
}

/// The web guide's `FIRST_RUN_ROUTE_COPY` titles.
pub fn route_title(route: &str) -> &'static str {
    match route {
        "input.text" | "output.text" => "Chat and text",
        "output.voice" => "Voice",
        "input.voice" => "Transcription",
        "output.image" => "Images",
        "input.image" => "Vision",
        "output.video" => "Video",
        _ => "Model",
    }
}

/// `recommended.recommended[]` of an availability payload, in order.
pub fn plan_rows(availability: &Value) -> Vec<PlanRow> {
    availability
        .get("recommended")
        .and_then(|r| r.get("recommended"))
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .map(|r| PlanRow {
                    route: s(r, "route").unwrap_or_default(),
                    provider: s(r, "provider").unwrap_or_default(),
                    artifact: s(r, "artifact").unwrap_or_default(),
                    status: s(r, "status").unwrap_or_default(),
                    tier: s(r, "tier"),
                    warning: s(r, "warning"),
                    evidence: s(r, "evidence"),
                    instruction: s(r, "instruction"),
                    engine_missing: r
                        .get("engine_missing")
                        .and_then(crate::store::EngineMissing::from_value),
                    fit_verdict: s(r, "fit_verdict")
                        .or_else(|| r.get("fit").and_then(|f| s(f, "verdict"))),
                    gpu_limit: r
                        .get("gpu_limit")
                        .or_else(|| r.get("fit").and_then(|f| f.get("gpu_limit")))
                        .and_then(GpuLimit::from_value),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// The web guide offers "Download all" while any recommended model is
/// absent and no Download-all group is running.
pub fn can_download_all(plan: &[PlanRow], group: Option<&GroupStatus>) -> bool {
    plan.iter().any(|r| r.status == "absent") && !group.map(GroupStatus::running).unwrap_or(false)
}

/// The Download-all parent job (`host_job_v1`, kind `download_group`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GroupStatus {
    pub job: String,
    /// running | completed | failed | cancelled
    pub status: String,
    /// resolving | downloading | stalled | done | failed | cancelled
    pub state: String,
    /// The gateway's own sentence ("Downloading 3 models · 1 of 3 ready · …").
    pub message: String,
    pub percent: Option<f64>,
    pub error: Option<String>,
    pub ended_reason: Option<String>,
    pub dry_run: bool,
    pub cancel_requested: bool,
    /// (name, state) per child — "supertonic supertonic-3", "downloading".
    pub files: Vec<(String, String)>,
}

impl GroupStatus {
    pub fn from_job(v: &Value) -> GroupStatus {
        GroupStatus {
            job: s(v, "job_id").or_else(|| s(v, "job")).unwrap_or_default(),
            status: s(v, "status").unwrap_or_default(),
            state: s(v, "state").unwrap_or_default(),
            message: s(v, "message").unwrap_or_default(),
            percent: v.get("percent").and_then(Value::as_f64),
            error: s(v, "error"),
            ended_reason: s(v, "ended_reason"),
            dry_run: v.get("dry_run").and_then(Value::as_bool).unwrap_or(false),
            cancel_requested: v
                .get("cancel_requested")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            files: v
                .get("files")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|f| {
                            (
                                s(f, "name").unwrap_or_default(),
                                s(f, "state").unwrap_or_default(),
                            )
                        })
                        .collect()
                })
                .unwrap_or_default(),
        }
    }

    /// Running = the gateway still reports the parent active.
    pub fn running(&self) -> bool {
        self.status == "running"
    }

    pub fn line(&self) -> String {
        let pct = match self.percent {
            Some(p) if self.running() => format!(" {p:.0}%"),
            _ => String::new(),
        };
        let mut out = format!("Download all{pct} — {}", self.message);
        if self.dry_run {
            out.push_str(" (dry run)");
        }
        if !self.running() {
            if let Some(r) = self.ended_reason.as_ref().or(self.error.as_ref()) {
                out.push_str(&format!(" — {r}"));
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_run_state_parses_and_decides_the_boot_mode() {
        let open = FirstRunState::from_value(&json!({
            "ok": true, "schema": "gateway_first_run_v1", "completed": false,
            "completed_at": null, "completed_by": null, "outcome": null
        }));
        assert!(!open.completed);
        assert!(
            first_run_auto_wizard(&open, true),
            "admin + not completed → wizard"
        );
        assert!(
            !first_run_auto_wizard(&open, false),
            "non-admin never gets the guide"
        );
        let done = FirstRunState::from_value(&json!({
            "completed": true, "completed_at": "2026-09-27T10:00:00Z",
            "completed_by": "admin", "outcome": "skipped"
        }));
        assert!(!first_run_auto_wizard(&done, true), "completed → browse");
        assert_eq!(done.outcome.as_deref(), Some("skipped"));
        assert!(done.line().contains("skipped"));
        assert!(done.line().contains("by admin"));
    }

    #[test]
    fn verify_requires_completed_and_the_requested_outcome() {
        let done = FirstRunState {
            completed: true,
            outcome: Some("finished".into()),
            ..FirstRunState::default()
        };
        assert!(first_run_verify("finished", &done).is_ok());
        assert!(
            first_run_verify("skipped", &done).is_err(),
            "outcome mismatch fails"
        );
        assert!(first_run_verify("finished", &FirstRunState::default()).is_err());
    }

    #[test]
    fn welcome_summary_reads_the_host_state_facts() {
        let v = json!({
            "host": {"host_id": "x", "host_name": "forge.local", "kind": "local"},
            "gateway": {
                "data_dir": "/data", "data_dir_source": "env", "auth_mode": "users",
                "service": {"installed": false, "mechanism": "launchd-agent"},
                "console_url": "http://127.0.0.1:18861/console",
                "first_run": {"completed": false}
            },
            "memory": {"ram": {"total_bytes": 137438953472u64}},
            "gpu": {"gpus": [{"name": "Apple M5 Max"}]}
        });
        let w = WelcomeSummary::from_host_state(&v);
        assert_eq!(w.computer.as_deref(), Some("forge.local"));
        assert_eq!(w.os, None, "no service platform: no OS line");
        // The OS comes from gateway.service.platform, like the web; the
        // host block has no OS field and older aliases are not read.
        let with_os = WelcomeSummary::from_host_state(&json!({
            "host": {"hostname": "alias", "os": "Darwin", "platform": "darwin"},
            "gateway": {"service": {"platform": "linux"}}
        }));
        assert_eq!(with_os.os.as_deref(), Some("Linux"));
        assert_eq!(
            with_os.computer, None,
            "only host.host_name names the computer"
        );
        let other = WelcomeSummary::from_host_state(&json!({
            "gateway": {"service": {"platform": "freebsd"}}
        }));
        assert_eq!(other.os, None, "unknown platform: nothing, never a guess");
        assert_eq!(w.ram_total_bytes, Some(137438953472));
        assert_eq!(w.gpus, vec!["Apple M5 Max".to_string()]);
        assert_eq!(w.first_run.as_ref().map(|f| f.completed), Some(false));
        let rows = w.rows();
        let get = |k: &str| rows.iter().find(|r| r.0 == k).cloned().unwrap();
        assert_eq!(get("Memory").1, "128.0 GB");
        assert_eq!(get("Sign-in").1, "User accounts");
        assert_eq!(get("Starts at login").1, "Not yet");
        assert!(get("Starts at login")
            .2
            .contains("abstractgateway service install"));
        assert!(get("Data folder").2.contains("(env)"));
        // Absent facts say unknown — never a guess.
        let empty = WelcomeSummary::from_host_state(&json!({}));
        let rows = empty.rows();
        assert_eq!(rows.iter().find(|r| r.0 == "Memory").unwrap().1, "Unknown");
        assert_eq!(
            rows.iter().find(|r| r.0 == "Starts at login").unwrap().1,
            "Unknown"
        );
    }

    #[test]
    fn plan_rows_carry_the_fit_warning_and_gate_download_all() {
        let v = json!({"recommended": {"recommended": [
            {"route": "input.text", "provider": "mlx", "artifact": "m-4bit",
             "status": "unknown", "tier": "memory >= 128 GiB",
             "warning": "may not fit"},
            {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3",
             "status": "absent"}
        ]}});
        let plan = plan_rows(&v);
        assert_eq!(plan.len(), 2);
        assert_eq!(plan[0].warning.as_deref(), Some("may not fit"));
        assert_eq!(plan[0].title(), "Chat and text");
        assert_eq!(plan[1].status_label(), "Not downloaded");
        assert!(
            can_download_all(&plan, None),
            "an absent row offers Download all"
        );
        let running = GroupStatus {
            status: "running".into(),
            ..GroupStatus::default()
        };
        assert!(
            !can_download_all(&plan, Some(&running)),
            "not while a group runs"
        );
        assert!(
            !can_download_all(&plan[..1], None),
            "unknown alone is not absent"
        );
    }

    #[test]
    fn group_status_folds_the_parent_job() {
        let g = GroupStatus::from_job(&json!({
            "schema": "host_job_v1", "kind": "download_group", "job_id": "grp_1",
            "status": "running", "state": "downloading", "percent": 12.6,
            "message": "Downloading 2 models · 0 of 2 ready", "dry_run": false,
            "files": [{"name": "supertonic supertonic-3", "state": "downloading"}]
        }));
        assert_eq!(g.job, "grp_1");
        assert!(g.running());
        assert!(g.line().contains("13%"));
        assert_eq!(g.files[0].1, "downloading");
        let ended = GroupStatus::from_job(&json!({
            "job_id": "grp_1", "status": "cancelled", "message": "Cancelled",
            "ended_reason": "admin cancelled this download in the console"
        }));
        assert!(!ended.running());
        assert!(ended.line().contains("admin cancelled"));
    }
}
