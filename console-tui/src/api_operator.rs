//! Operator-control routes (web-console parity): gateway host controls,
//! workflow import/reload, the curated skills reseed, the WAN address
//! lookup and the caller's own workspace policy.
//!
//! A child module of `api` (one `#[path]` line there) so it reaches the
//! client's private transport (`get`, `send`, agents, error mapping)
//! without widening any of it.

use serde_json::{json, Value};

use super::{err_from_ureq, ApiResult, GatewayClient};

impl GatewayClient {
    // ---- gateway host (`routes/gateway.py` host control) ----------------

    /// `GET /host/runner` — paused?, by whom, ticks in flight, and what
    /// this process can do to itself (restart/shutdown capabilities).
    pub fn host_runner(&self) -> ApiResult<Value> {
        self.get("/host/runner", false)
    }

    /// `GET /host/tray` — the desktop tray helper and why it is not shown.
    pub fn host_tray(&self) -> ApiResult<Value> {
        self.get("/host/tray", false)
    }

    /// `POST /host/pause` (admin) — answers the runner payload.
    pub fn host_pause(&self) -> ApiResult<Value> {
        self.send("POST", "/host/pause", &json!({}), false)
    }

    /// `POST /host/resume` (admin) — answers the runner payload.
    pub fn host_resume(&self) -> ApiResult<Value> {
        self.send("POST", "/host/resume", &json!({}), false)
    }

    /// `POST /host/restart` (admin). 409 = cannot relaunch here (reason).
    pub fn host_restart(&self) -> ApiResult<Value> {
        self.send("POST", "/host/restart", &json!({"reason": "console"}), false)
    }

    /// `POST /host/shutdown` (admin).
    pub fn host_shutdown(&self) -> ApiResult<Value> {
        self.send("POST", "/host/shutdown", &json!({"reason": "console"}), false)
    }

    /// `GET /host/update` (admin) — install kind, last check, job state.
    pub fn host_update(&self) -> ApiResult<Value> {
        self.get("/host/update", false)
    }

    /// `POST /host/update/check` (admin) — asks PyPI (5 s server-side);
    /// offline is an in-band answer. Slow agent: the check blocks.
    pub fn host_update_check(&self) -> ApiResult<Value> {
        self.send("POST", "/host/update/check", &json!({}), true)
    }

    /// `POST /host/update/start` (admin) — runs the upgrade in the
    /// background; 409 when it cannot upgrade in place or a job runs.
    pub fn host_update_start(&self) -> ApiResult<Value> {
        self.send("POST", "/host/update/start", &json!({}), false)
    }

    /// The (base URL, token) this client talks with — the restart watcher
    /// re-probes the SAME gateway with the SAME credentials once it is back.
    pub fn credentials(&self) -> (String, Option<String>) {
        (self.base_url.clone(), self.token.clone())
    }

    // ---- workflows --------------------------------------------------------

    /// `POST /bundles/upload` (multipart: file + overwrite + reload) — the
    /// web console's Import. `ok:false` + `loaded:false` = installed but
    /// NOT served (the `skipped.reason` says why).
    pub fn upload_bundle(
        &self,
        filename: &str,
        bytes: &[u8],
        overwrite: bool,
        reload: bool,
    ) -> ApiResult<Value> {
        let path = "/bundles/upload";
        let boundary = format!(
            "----abstractgateway-console-{:x}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        );
        let body = multipart_body(
            &boundary,
            filename,
            bytes,
            &[
                ("overwrite", if overwrite { "true" } else { "false" }),
                ("reload", if reload { "true" } else { "false" }),
            ],
        );
        let req = self.with_auth(
            self.slow_agent
                .post(&self.url(path))
                .set("Accept", "application/json"),
        );
        let resp = req
            .set(
                "Content-Type",
                &format!("multipart/form-data; boundary={boundary}"),
            )
            .send_bytes(&body)
            .map_err(|e| err_from_ureq(path, e))?;
        Self::read_json(path, resp)
    }

    /// `POST /bundles/reload` — re-read the bundles folder from disk.
    pub fn reload_bundles(&self) -> ApiResult<Value> {
        self.send("POST", "/bundles/reload", &json!({}), true)
    }

    // ---- runtime knobs ----------------------------------------------------

    /// `POST /admin/skills/reseed` (admin) — refresh the gateway's own copy
    /// of the curated skills shelf; answers the SeedReport.
    pub fn reseed_skills(&self) -> ApiResult<Value> {
        self.send("POST", "/admin/skills/reseed", &json!({}), true)
    }

    // ---- network ----------------------------------------------------------

    /// `GET /network?lookup_public=1` — the network status plus ONE
    /// outbound WAN-address lookup (admin; internet mode only — otherwise
    /// `discovery.public_note` says why it did not run).
    pub fn network_lookup_public(&self) -> ApiResult<Value> {
        self.get("/network?lookup_public=1", true)
    }

    // ---- the caller's own workspace policy --------------------------------

    /// `GET /workspace/policy/self` — any principal, their own entry.
    pub fn my_workspace_policy(&self) -> ApiResult<Value> {
        self.get("/workspace/policy/self", false)
    }

    /// `PUT /workspace/policy/self` — `{}` clears back to inherited.
    pub fn save_my_workspace_policy(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/workspace/policy/self", body, false)
    }
}

/// A `multipart/form-data` body: one `file` part (octet-stream) followed
/// by plain text fields — the shape the web console's FormData posts.
pub fn multipart_body(
    boundary: &str,
    filename: &str,
    bytes: &[u8],
    fields: &[(&str, &str)],
) -> Vec<u8> {
    // A quote or CR/LF in a file name would break the header line.
    let safe: String = filename
        .chars()
        .map(|c| if matches!(c, '"' | '\r' | '\n') { '_' } else { c })
        .collect();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len() + 512);
    out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    out.extend_from_slice(
        format!("Content-Disposition: form-data; name=\"file\"; filename=\"{safe}\"\r\n").as_bytes(),
    );
    out.extend_from_slice(b"Content-Type: application/octet-stream\r\n\r\n");
    out.extend_from_slice(bytes);
    out.extend_from_slice(b"\r\n");
    for (name, value) in fields {
        out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        out.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").as_bytes(),
        );
    }
    out.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn multipart_body_carries_the_file_and_fields() {
        let body = multipart_body("B", "a\"b.flow", b"\x00PK", &[("overwrite", "false"), ("reload", "true")]);
        let text = String::from_utf8_lossy(&body);
        assert!(text.starts_with("--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a_b.flow\"\r\n"));
        assert!(body.windows(3).any(|w| w == b"\x00PK"), "the bytes ride verbatim");
        assert!(text.contains("name=\"overwrite\"\r\n\r\nfalse\r\n"));
        assert!(text.contains("name=\"reload\"\r\n\r\ntrue\r\n"));
        assert!(text.ends_with("--B--\r\n"));
    }
}
