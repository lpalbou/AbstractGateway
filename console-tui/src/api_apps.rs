//! Browser apps client: `/api/gateway/apps*` (routes/apps.py).
//!
//! Declared as a child of `api` (`#[path]` in api.rs) so it reuses the
//! private GET/POST plumbing (retry law, error taxonomy) without widening
//! it. Every call is the real route; a refusal comes back as the route's
//! own `{ok:false, reason, message, hint, command?}` body on the error
//! (`ApiError::body`), which `store::apps::app_error_note` words.

use serde_json::{json, Value};

use super::{urlencode, ApiResult, GatewayClient};

/// The route's own ceiling for `GET /apps/{id}/logs?tail=N` (routes/apps.py
/// clamps to 1..=5000); the log panel says so out loud when it reaches it.
pub const APP_LOG_MAX: u32 = 5000;

impl GatewayClient {
    /// `GET /apps?latest=` — Node.js runtime + one row per app. `latest`
    /// asks the npm registry and the terminal app's release for newer
    /// versions (seconds, network): the web console's "Check again".
    pub fn apps_overview(&self, latest: bool) -> ApiResult<Value> {
        self.get(&format!("/apps?latest={latest}"), latest)
    }

    /// `GET /apps/jobs/{id}` — one install/update job (404 once the
    /// gateway restarted: jobs live in its memory).
    pub fn apps_job(&self, job_id: &str) -> ApiResult<Value> {
        self.get(&format!("/apps/jobs/{}", urlencode(job_id)), false)
    }

    /// `POST /apps/jobs/{id}/cancel` (admin). The job stops at its next
    /// checkpoint: the answer usually still says `running`.
    pub fn apps_job_cancel(&self, job_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &format!("/apps/jobs/{}/cancel", urlencode(job_id)),
            &json!({}),
            false,
        )
    }

    /// `POST /apps/{id}/install {}` (admin) — the console's Install: the
    /// browser app and, when the row's `install_parts` has "tui", its
    /// terminal app, as ONE job; it starts nothing.
    pub fn apps_install(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/install", urlencode(app_id)), &json!({}), true)
    }

    /// `POST /apps/{id}/update {}` (admin) — a job; a running app restarts
    /// on the new version.
    pub fn apps_update(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/update", urlencode(app_id)), &json!({}), true)
    }

    /// `POST /apps/{id}/launch {}` (admin) — start a browser app (answer
    /// `{app: row}`), or open a desktop app on the gateway computer's
    /// screen (answer `{message}`).
    pub fn apps_launch(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/launch", urlencode(app_id)), &json!({}), true)
    }

    /// `POST /apps/{id}/stop {}` (admin). 409 `started_outside_gateway`
    /// for an app the gateway did not start.
    pub fn apps_stop(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/stop", urlencode(app_id)), &json!({}), true)
    }

    /// `POST /apps/{id}/open {path?}` — a one-time signed-in link
    /// (`open_url`, relative to the gateway) for a RUNNING app.
    pub fn apps_open(&self, app_id: &str, path: Option<&str>) -> ApiResult<Value> {
        let body = match path {
            Some(p) => json!({ "path": p }),
            None => json!({}),
        };
        self.send("POST", &format!("/apps/{}/open", urlencode(app_id)), &body, false)
    }

    /// `GET /apps/{id}/logs?tail=N` (admin) → `{path, lines}`.
    pub fn apps_logs(&self, app_id: &str, tail: u32) -> ApiResult<Value> {
        let tail = tail.clamp(1, APP_LOG_MAX);
        self.get(&format!("/apps/{}/logs?tail={tail}", urlencode(app_id)), false)
    }

    /// `POST /apps/runtime/install {}` (admin) — Node.js into the gateway's
    /// own folder, as a job (`job: null` + `message` when it is already there).
    pub fn apps_runtime_install(&self) -> ApiResult<Value> {
        self.send("POST", "/apps/runtime/install", &json!({}), true)
    }

    /// `POST /apps/{id}/install-tui {}` (admin) — install or update the
    /// prebuilt terminal app, as a job. 409 `toolchain_required` + `command`.
    pub fn apps_install_tui(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/install-tui", urlencode(app_id)), &json!({}), true)
    }

    /// `POST /apps/{id}/launch-tui {}` (admin) — a new terminal window ON
    /// THE GATEWAY MACHINE, signed in. From anywhere else: 409
    /// `not_on_gateway_machine` with `command` + `signin_command` to copy.
    pub fn apps_launch_tui(&self, app_id: &str) -> ApiResult<Value> {
        self.send("POST", &format!("/apps/{}/launch-tui", urlencode(app_id)), &json!({}), false)
    }
}
