//! The Workflows page's routes beyond the list/archive/download/upload
//! ones in `api.rs` — the SAME the web console calls:
//! `GET /bundles?…&include_archived=`, `POST /bundles/{id}/unarchive`,
//! `PUT /admin/workflows/{id}/availability`, `GET|POST
//! /admin/runtime-config` (the default workflow per app, streamed replies).

use serde_json::{json, Value};

use super::{urlencode, ApiResult, GatewayClient};

impl GatewayClient {
    /// The web's list read (`loadWorkflows`): every version, drafts and
    /// archived on request, deprecated always.
    pub fn bundles_page(&self, include_drafts: bool, include_archived: bool) -> ApiResult<Value> {
        let d = if include_drafts { "1" } else { "0" };
        let a = if include_archived { "1" } else { "0" };
        self.get(
            &format!(
                "/bundles?all_versions=true&include_drafts={d}&include_deprecated=true&include_archived={a}"
            ),
            true,
        )
    }

    /// `POST /bundles/{id}/unarchive` (`{}` = every version).
    pub fn unarchive_bundle(&self, bundle_id: &str, version: &str) -> ApiResult<Value> {
        let body = if version.is_empty() {
            json!({})
        } else {
            json!({ "bundle_version": version })
        };
        self.send(
            "POST",
            &format!("/bundles/{}/unarchive", urlencode(bundle_id)),
            &body,
            false,
        )
    }

    /// `PUT /admin/workflows/{id}/availability` `{available}` (admin).
    pub fn set_workflow_availability(&self, bundle_id: &str, available: bool) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!("/admin/workflows/{}/availability", urlencode(bundle_id)),
            &json!({ "available": available }),
            false,
        )
    }

    /// `GET /admin/runtime-config` (readable by any signed-in user).
    pub fn runtime_config_raw(&self) -> ApiResult<Value> {
        self.get("/admin/runtime-config", false)
    }

    /// `POST /admin/runtime-config` (admin) with the web's body.
    pub fn post_runtime_config(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/admin/runtime-config", body, false)
    }
}
