//! `GatewayClient` methods behind the shared screens' OPTIONAL verbs
//! (`abstractcore-console` 0.3 `ConsoleTransport`): the same gateway
//! routes the web console calls (console_ui.py engines panel,
//! console_catalog.py Hugging Face search, console_ui.py downloads).
//! Bodies are returned verbatim; `transport_http` maps errors.

use serde_json::{json, Value};

use super::{urlencode, ApiResult, GatewayClient};

impl GatewayClient {
    /// `POST /engines/{id}/install {dry_run, location}` (admin). A dry
    /// run answers the PLAN for that location (`plan.target`,
    /// `plan.needs_admin`) and needs no install permission; a real one
    /// answers the `engine_install_job_v1` job (403 when installs are
    /// off, 409 while another engine installs).
    pub fn engine_install_at(&self, id: &str, dry_run: bool, location: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/engines/{}/install", urlencode(id)),
            &json!({"dry_run": dry_run, "location": location}),
            dry_run,
        )
    }

    /// `POST /engines/jobs/{id}/continue {action?}` (admin): resume a
    /// job paused in `needs_admin` / `needs_tools`. `None` = the job's
    /// first `continue_actions` entry (the gateway's default). 409 when
    /// the job is not paused, 404 when unknown.
    pub fn engine_job_continue(&self, job_id: &str, action: Option<&str>) -> ApiResult<Value> {
        let body = match action {
            Some(a) => json!({ "action": a }),
            None => json!({}),
        };
        self.send(
            "POST",
            &format!("/engines/jobs/{}/continue", urlencode(job_id)),
            &body,
            true,
        )
    }

    /// `POST /engines/{id}/start|stop` (admin): Ollama / LM Studio
    /// servers; waits until the server answers (slow). 409 for an engine
    /// that is not a server.
    pub fn engine_server(&self, id: &str, verb: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/engines/{}/{}", urlencode(id), urlencode(verb)),
            &json!({}),
            true,
        )
    }

    /// `GET /models/catalog?q=&hub=true` — contract C enriched from the
    /// Hugging Face API, with hub search rows for `q` (slow: a network
    /// call on the gateway host, cached 24 h there).
    pub fn models_catalog_hub(
        &self,
        q: &str,
        engine: Option<&str>,
        fits: bool,
    ) -> ApiResult<Value> {
        let mut path = format!("/models/catalog?q={}&hub=true", urlencode(q));
        if let Some(e) = engine.filter(|e| !e.is_empty()) {
            path.push_str(&format!("&engine={}", urlencode(e)));
        }
        path.push_str(if fits { "&fits=1" } else { "&fits=0" });
        self.get(&path, true)
    }

    /// `POST /models/download/{id}/cancel {"via":"console"}` (admin): the
    /// web console's download cancel — a `grp_…` group cancels every
    /// running child, and `via: console` makes the job's final words
    /// say a person cancelled it in the console. Answers `{ok, job}`.
    pub fn cancel_model_download(&self, job_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/models/download/{}/cancel", urlencode(job_id)),
            &json!({"via": "console"}),
            true,
        )
    }

    /// `GET /models/downloads` — every download job of this gateway
    /// process, newest first (`{ok, jobs}`; groups carry `children`).
    pub fn models_downloads(&self) -> ApiResult<Value> {
        self.get("/models/downloads", false)
    }
}
