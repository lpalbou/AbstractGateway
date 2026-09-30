//! Per-user email routes (framework backlog 0992): the caller's own
//! mailbox (`/me/email*`), notification preferences (`/me/notifications*`)
//! and the admin's per-user switch (`/admin/users/{id}/email`, status and
//! on/off only — administrators never read mail).
//!
//! A child module of `api` (one `#[path]` line there) so it reaches the
//! client's private transport without widening it.

use serde_json::{json, Value};

use super::{urlencode, ApiResult, GatewayClient};

impl GatewayClient {
    /// `GET /me/email` — settings and status (never a secret).
    pub fn my_email(&self) -> ApiResult<Value> {
        self.get("/me/email", false)
    }

    /// `PUT /me/email` — test, then store (the password travels in the body only).
    pub fn connect_my_email(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/me/email", body, true)
    }

    /// `POST /me/email/test` — per-leg `{imap, smtp, ok}`.
    pub fn test_my_email(&self) -> ApiResult<Value> {
        self.send("POST", "/me/email/test", &json!({}), true)
    }

    /// `DELETE /me/email` — credentials and cursor deleted.
    pub fn disconnect_my_email(&self) -> ApiResult<Value> {
        self.delete("/me/email")
    }

    /// `PUT /me/email/policy` — `{mode, entries}`.
    pub fn set_my_email_policy(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/me/email/policy", body, false)
    }

    /// `PUT /me/email/limits` — `{per_hour, per_day}`.
    pub fn set_my_email_limits(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/me/email/limits", body, false)
    }

    /// `PUT /me/email/enabled` — the user's own switch.
    pub fn set_my_email_enabled(&self, enabled: bool) -> ApiResult<Value> {
        self.send(
            "PUT",
            "/me/email/enabled",
            &json!({ "enabled": enabled }),
            false,
        )
    }

    /// `PUT /me/email/agent-tools` — the agents' email tools (default off).
    pub fn set_my_email_agent_tools(&self, enabled: bool) -> ApiResult<Value> {
        self.send(
            "PUT",
            "/me/email/agent-tools",
            &json!({ "enabled": enabled }),
            true,
        )
    }

    /// `GET /me/notifications`.
    pub fn my_notifications(&self) -> ApiResult<Value> {
        self.get("/me/notifications", false)
    }

    /// `PUT /me/notifications` — `{email: {event: bool}}`.
    pub fn set_my_notifications(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/me/notifications", body, false)
    }

    /// `POST /me/notifications/test` — `{ok, state, error?}`.
    pub fn test_my_notifications(&self) -> ApiResult<Value> {
        self.send("POST", "/me/notifications/test", &json!({}), true)
    }

    /// `POST /me/email/oauth/start` — device code or loopback link.
    pub fn my_email_oauth_start(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", "/me/email/oauth/start", body, true)
    }

    /// `POST /me/email/oauth/finish` — waits up to `wait_s` (≤ 60) for the approval.
    pub fn my_email_oauth_finish(&self, flow_id: &str, wait_s: f64) -> ApiResult<Value> {
        self.send(
            "POST",
            "/me/email/oauth/finish",
            &json!({"flow_id": flow_id, "wait_s": wait_s}),
            true,
        )
    }

    /// `POST /me/email/oauth/cancel`.
    pub fn my_email_oauth_cancel(&self, flow_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            "/me/email/oauth/cancel",
            &json!({"flow_id": flow_id}),
            false,
        )
    }

    /// `PUT /admin/users/{id}/email` (admin) — turn email on/off for a user.
    pub fn set_user_email_enabled(
        &self,
        user_id: &str,
        tenant_id: &str,
        enabled: bool,
    ) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!(
                "/admin/users/{}/email?tenant_id={}",
                urlencode(user_id),
                urlencode(tenant_id)
            ),
            &json!({ "enabled": enabled }),
            false,
        )
    }

    /// `GET /admin/users/{id}/email` (admin) — status only.
    pub fn user_email_status(&self, user_id: &str, tenant_id: &str) -> ApiResult<Value> {
        self.get(
            &format!(
                "/admin/users/{}/email?tenant_id={}",
                urlencode(user_id),
                urlencode(tenant_id)
            ),
            false,
        )
    }
}
