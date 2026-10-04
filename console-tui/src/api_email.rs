//! Per-user email routes (framework backlog 0992): the caller's own
//! mailbox (`/me/email*`), notification preferences (`/me/notifications*`)
//! and the admin's per-user status and override reset
//! (`/admin/users/{id}/email` — administrators never read mail).
//!
//! A child module of `api` (one `#[path]` line there) so it reaches the
//! client's private transport without widening it.

use serde_json::{json, Value};

use super::{urlencode, ApiResult, GatewayClient};

impl GatewayClient {
    /// `GET /me/email` — settings and status (never a secret).
    pub fn my_email(&self) -> ApiResult<Value> {
        self.get(&self.me_path("/me/email"), false)
    }

    /// `PUT /me/email` — test, then store (the password travels in the body only).
    pub fn connect_my_email(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &self.me_path("/me/email"), body, true)
    }

    /// `POST /me/email/discover {"address"}` — the mailbox's IMAP/SMTP
    /// servers found from the address (CONTRACT §5.3; nothing is stored).
    pub fn discover_my_email(&self, address: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &self.me_path("/me/email/discover"),
            &json!({ "address": address }),
            true,
        )
    }

    /// `PUT /me/email/address {"address"}` — my Email address (sign-in
    /// codes, notifications; not a mailbox). `""` clears it.
    pub fn set_my_email_address(&self, address: &str) -> ApiResult<Value> {
        self.send(
            "PUT",
            &self.me_path("/me/email/address"),
            &json!({ "address": address }),
            false,
        )
    }

    /// `PUT /me/email/notifications {job_failed?, approval_needed?}` — the
    /// two notification switches; answers like `GET /me/email`.
    pub fn set_my_notification_switches(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &self.me_path("/me/email/notifications"), body, false)
    }

    /// `GET /session/recovery` (public) — is "Forgot your token?" offered.
    pub fn recovery_available(&self) -> ApiResult<Value> {
        self.get("/session/recovery", false)
    }

    /// `POST /session/recovery/request` (public) — email a code; the answer
    /// is honest (`sent`, `to`, `message`, `reason_code`, `retry_after_s`).
    pub fn recovery_request(
        &self,
        user_id: &str,
        tenant_id: &str,
        purpose: &str,
    ) -> ApiResult<Value> {
        self.send(
            "POST",
            "/session/recovery/request",
            &json!({"user_id": user_id, "tenant_id": tenant_id, "purpose": purpose}),
            true,
        )
    }

    /// `POST /session/recovery/redeem` (public) — the emailed code for a
    /// session; with `reset_token` the answer carries the new `token`.
    pub fn recovery_redeem(
        &self,
        user_id: &str,
        tenant_id: &str,
        purpose: &str,
        code: &str,
    ) -> ApiResult<Value> {
        self.send(
            "POST",
            "/session/recovery/redeem",
            &json!({"user_id": user_id, "tenant_id": tenant_id, "purpose": purpose, "code": code}),
            true,
        )
    }

    /// `POST /me/email/test` — per-leg `{imap, smtp, ok}`.
    pub fn test_my_email(&self) -> ApiResult<Value> {
        self.send("POST", &self.me_path("/me/email/test"), &json!({}), true)
    }

    /// `DELETE /me/email` — credentials and cursor deleted.
    pub fn disconnect_my_email(&self) -> ApiResult<Value> {
        self.delete(&self.me_path("/me/email"))
    }

    /// `PUT /me/email/policy` — `{mode, entries}`.
    pub fn set_my_email_policy(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &self.me_path("/me/email/policy"), body, false)
    }

    /// `PUT /me/email/limits` — `{per_hour, per_day}`.
    pub fn set_my_email_limits(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &self.me_path("/me/email/limits"), body, false)
    }

    /// `PUT /me/email/folder {"folder"}` — the IMAP folder read (empty =
    /// INBOX); 404 `email_not_configured` without a mailbox.
    pub fn set_my_email_folder(&self, folder: &str) -> ApiResult<Value> {
        self.send(
            "PUT",
            &self.me_path("/me/email/folder"),
            &json!({ "folder": folder }),
            false,
        )
    }

    /// `PUT /me/email/enabled` — the user's own switch.
    pub fn set_my_email_enabled(&self, enabled: bool) -> ApiResult<Value> {
        self.send(
            "PUT",
            &self.me_path("/me/email/enabled"),
            &json!({ "enabled": enabled }),
            false,
        )
    }

    /// `PUT /me/email/agent-tools` — the agents' email tools (default off).
    pub fn set_my_email_agent_tools(&self, enabled: bool) -> ApiResult<Value> {
        self.send(
            "PUT",
            &self.me_path("/me/email/agent-tools"),
            &json!({ "enabled": enabled }),
            true,
        )
    }

    /// `GET /me/notifications`.
    pub fn my_notifications(&self) -> ApiResult<Value> {
        self.get(&self.me_path("/me/notifications"), false)
    }

    /// `PUT /me/notifications` — `{email: {event: bool}}`.
    pub fn set_my_notifications(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", &self.me_path("/me/notifications"), body, false)
    }

    /// `POST /me/notifications/test` — `{ok, state, error?}`.
    pub fn test_my_notifications(&self) -> ApiResult<Value> {
        self.send(
            "POST",
            &self.me_path("/me/notifications/test"),
            &json!({}),
            true,
        )
    }

    /// `POST /me/email/oauth/start` — device code or loopback link.
    pub fn my_email_oauth_start(&self, body: &Value) -> ApiResult<Value> {
        self.send("POST", &self.me_path("/me/email/oauth/start"), body, true)
    }

    /// `POST /me/email/oauth/finish` — waits up to `wait_s` (≤ 60) for the approval.
    pub fn my_email_oauth_finish(&self, flow_id: &str, wait_s: f64) -> ApiResult<Value> {
        self.send(
            "POST",
            &self.me_path("/me/email/oauth/finish"),
            &json!({"flow_id": flow_id, "wait_s": wait_s}),
            true,
        )
    }

    /// `POST /me/email/oauth/cancel`.
    pub fn my_email_oauth_cancel(&self, flow_id: &str) -> ApiResult<Value> {
        self.send(
            "POST",
            &self.me_path("/me/email/oauth/cancel"),
            &json!({"flow_id": flow_id}),
            false,
        )
    }

    /// `PUT /admin/users/{id}/email {"inherit": [...]}` (admin) — clear an
    /// old per-user override so the user follows the gateway-wide
    /// "Mailboxes for users" switch again (a one-shot Reset; the console
    /// never creates per-user overrides).
    pub fn reset_user_mailbox_override(&self, user_id: &str, tenant_id: &str) -> ApiResult<Value> {
        self.send(
            "PUT",
            &format!(
                "/admin/users/{}/email?tenant_id={}",
                urlencode(user_id),
                urlencode(tenant_id)
            ),
            &json!({ "inherit": ["email", "email_agent_tools"] }),
            false,
        )
    }

    /// `GET /admin/email/capabilities` (admin) — the gateway-wide email defaults.
    pub fn email_capabilities(&self) -> ApiResult<Value> {
        self.get("/admin/email/capabilities", false)
    }

    /// `PUT /admin/email/capabilities` (admin).
    pub fn set_email_capabilities(&self, body: &Value) -> ApiResult<Value> {
        self.send("PUT", "/admin/email/capabilities", body, false)
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
