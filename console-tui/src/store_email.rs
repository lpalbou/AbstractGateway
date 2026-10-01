//! "My email" state (framework backlog 0992, web parity: the Users tab's
//! "My email" section) — the caller's OWN mailbox (`GET /me/email`,
//! `email_settings_v1`) and notification preferences
//! (`GET /me/notifications`), plus the admin's per-user mailbox column.
//!
//! Declared as `store::email` (one `mod` line in store.rs); the signals
//! ride `OperatorStore` so the shared store only grows by three fields.
//! Parsing and body building are pure and unit-tested here.

use serde_json::{json, Value};

fn s(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_default()
}

fn b(v: &Value, key: &str) -> Option<bool> {
    v.get(key).and_then(Value::as_bool)
}

fn n(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// One mail server leg (IMAP read / SMTP send).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MailServer {
    pub host: String,
    pub port: Option<i64>,
    pub security: String,
    pub folder: String,
}

impl MailServer {
    fn from_value(v: Option<&Value>) -> Option<MailServer> {
        let v = v.filter(|v| v.is_object())?;
        let host = s(v, "host");
        if host.is_empty() {
            return None;
        }
        Some(MailServer {
            host,
            port: n(v, "port"),
            security: s(v, "security"),
            folder: s(v, "folder"),
        })
    }

    pub fn text(&self) -> String {
        match self.port {
            Some(p) => format!("{}:{} {}", self.host, p, self.security),
            None => format!("{} {}", self.host, self.security),
        }
    }
}

/// A typed failure the gateway recorded (`{code, cause, fix}`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MailError {
    pub code: String,
    pub cause: String,
    pub fix: String,
}

impl MailError {
    fn from_value(v: Option<&Value>) -> Option<MailError> {
        let v = v.filter(|v| v.is_object())?;
        Some(MailError {
            code: s(v, "code"),
            cause: s(v, "cause"),
            fix: s(v, "fix"),
        })
    }

    pub fn text(&self) -> String {
        let what = if self.cause.is_empty() {
            self.code.clone()
        } else {
            self.cause.clone()
        };
        if self.fix.is_empty() {
            what
        } else {
            format!("{what} Fix: {}", self.fix)
        }
    }
}

/// `GET /me/email` (`email_settings_v1` + the gateway's fields).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MyEmail {
    pub configured: bool,
    pub enabled: bool,
    pub admin_enabled: bool,
    pub effective_enabled: bool,
    pub address: String,
    pub display_name: String,
    pub username: String,
    pub auth_kind: String,
    pub imap: Option<MailServer>,
    pub smtp: Option<MailServer>,
    pub secret_storage: String,
    pub policy_mode: String,
    pub policy_entries: Vec<String>,
    pub per_hour: Option<i64>,
    pub per_day: Option<i64>,
    pub used_last_hour: i64,
    pub used_last_day: i64,
    pub last_test: String,
    pub imap_test: String,
    pub smtp_test: String,
    pub last_error: Option<MailError>,
    pub watcher_state: String,
    pub watcher_last_poll: String,
    pub admin_disabled: String,
    pub notices: Vec<String>,
    /// The user's "Agent email tools" toggle (default off) and whether it is in force.
    pub agent_tools_enabled: bool,
    pub agent_tools_available: bool,
    pub agent_tools_active: bool,
    pub agent_tools_reason: String,
    /// Which account this is (the admin's gateway account vs AbstractCore's local one).
    pub store_label: String,
    /// `registered_address`: "self" for runs (the stored email address,
    /// else the connected mailbox's own); empty before the field existed.
    pub registered_address: String,
    /// `email_address`: the user's Email address as stored ("" = none) —
    /// the account page's field. `None` on a gateway that predates it.
    pub stored_email_address: Option<String>,
    /// The two notification switches (`notifications`), `None` on a gateway
    /// whose `/me/email` does not carry them (read from `/me/notifications`).
    pub notify_job_failed: Option<bool>,
    pub notify_approval_needed: Option<bool>,
    /// Why the notification switches can't be used (`notifications_unavailable_reason`).
    pub notifications_unavailable_reason: String,
    /// Why the Agent email tools switch can't be used (`agent_tools.unavailable_reason`).
    pub agent_tools_unavailable_reason: String,
    /// The admin's "Mailboxes for users" as it applies to this user (`email_available`).
    pub email_available: Option<bool>,
    /// `oauth_providers`: `(id, available, reason)` for the Google / Microsoft tabs.
    pub oauth_providers: Vec<(String, bool, String)>,
}

/// The reasons the account page shows on an unavailable switch (DESIGN §6),
/// used when the gateway's answer carries none.
pub const REASON_CONNECT_MAILBOX: &str = "Connect a mailbox first.";
pub const REASON_ADMIN_MAILBOXES_OFF: &str = "Your admin turned mailboxes off.";
pub const REASON_ADMIN_AGENT_TOOLS_OFF: &str = "Your admin turned agent email tools off.";
pub const REASON_MAILBOX_NOT_IN_USE: &str =
    "Switch the mailbox\u{2019}s \u{201c}Active\u{201d} on first (Mailbox card).";

fn leg_text(v: Option<&Value>) -> String {
    match v {
        None => "-".into(),
        Some(leg) => match leg.get("ok") {
            Some(Value::Bool(true)) => "ok".into(),
            Some(Value::Bool(false)) => {
                let cause = s(leg, "cause");
                format!(
                    "failed: {}",
                    if cause.is_empty() {
                        s(leg, "code")
                    } else {
                        cause
                    }
                )
            }
            Some(Value::Null) => "not configured".into(),
            _ => "-".into(),
        },
    }
}

impl MyEmail {
    pub fn from_value(v: &Value) -> MyEmail {
        let policy = v.get("policy").cloned().unwrap_or(Value::Null);
        let limits = v.get("limits").cloned().unwrap_or(Value::Null);
        let status = v.get("status").cloned().unwrap_or(Value::Null);
        let legs = status.get("legs").cloned().unwrap_or(Value::Null);
        let watcher = v.get("watcher").cloned().unwrap_or(Value::Null);
        let agent = v.get("agent_tools").cloned().unwrap_or(Value::Null);
        let admin_disabled = v
            .get("admin_disabled")
            .map(|d| {
                format!("{} {}", s(d, "cause"), s(d, "fix"))
                    .trim()
                    .to_string()
            })
            .unwrap_or_default();
        MyEmail {
            configured: b(v, "configured").unwrap_or(false),
            enabled: b(v, "enabled").unwrap_or(false),
            admin_enabled: b(v, "admin_enabled").unwrap_or(true),
            effective_enabled: b(v, "effective_enabled").unwrap_or(false),
            address: s(v, "address"),
            display_name: s(v, "display_name"),
            username: s(v, "username"),
            auth_kind: s(v, "auth_kind"),
            imap: MailServer::from_value(v.get("imap")),
            smtp: MailServer::from_value(v.get("smtp")),
            secret_storage: s(v, "secret_storage"),
            policy_mode: {
                let m = s(&policy, "mode");
                if m.is_empty() {
                    "allowlist".into()
                } else {
                    m
                }
            },
            policy_entries: policy
                .get("entries")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
            per_hour: n(&limits, "per_hour"),
            per_day: n(&limits, "per_day"),
            used_last_hour: n(&limits, "used_last_hour").unwrap_or(0),
            used_last_day: n(&limits, "used_last_day").unwrap_or(0),
            last_test: s(&status, "last_test"),
            imap_test: leg_text(legs.get("imap")),
            smtp_test: leg_text(legs.get("smtp")),
            last_error: MailError::from_value(status.get("last_error")),
            watcher_state: {
                let w = s(&watcher, "state");
                if w.is_empty() {
                    "idle".into()
                } else {
                    w
                }
            },
            watcher_last_poll: s(&watcher, "last_poll"),
            admin_disabled,
            agent_tools_enabled: b(&agent, "on")
                .or_else(|| b(&agent, "enabled"))
                .unwrap_or(false),
            agent_tools_available: b(&agent, "available").unwrap_or(false),
            agent_tools_active: b(&agent, "active").unwrap_or(false),
            store_label: v.get("store").map(|st| s(st, "label")).unwrap_or_default(),
            registered_address: s(v, "registered_address"),
            stored_email_address: v
                .get("email_address")
                .and_then(Value::as_str)
                .map(str::to_string),
            notify_job_failed: v.get("notifications").and_then(|n| b(n, "job_failed")),
            notify_approval_needed: v.get("notifications").and_then(|n| b(n, "approval_needed")),
            notifications_unavailable_reason: s(v, "notifications_unavailable_reason"),
            agent_tools_unavailable_reason: s(&agent, "unavailable_reason"),
            email_available: b(v, "email_available"),
            oauth_providers: v
                .get("oauth_providers")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|p| {
                            (
                                s(p, "id"),
                                b(p, "available").unwrap_or(false),
                                s(p, "reason"),
                            )
                        })
                        .collect()
                })
                .unwrap_or_default(),
            agent_tools_reason: s(&agent, "reason"),
            notices: v
                .get("notices")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
        }
    }

    /// The same words as the web console's state line.
    pub fn state_label(&self) -> &'static str {
        if !self.configured {
            "not connected"
        } else if !self.admin_enabled {
            "turned off by an administrator"
        } else if !self.enabled {
            "off"
        } else if self.last_error.is_some() {
            "needs action"
        } else {
            "connected"
        }
    }

    /// The web console's agent-tools line.
    pub fn agent_tools_text(&self) -> String {
        if self.agent_tools_active {
            "on: your agents and workflows have the email tools (policy, limits and approval still apply)".into()
        } else if self.agent_tools_reason.is_empty() {
            "off".into()
        } else {
            format!("off — {}", self.agent_tools_reason)
        }
    }

    pub fn credentials_text(&self) -> &'static str {
        match self.secret_storage.as_str() {
            "os-keychain" => "encrypted, key in the OS keychain",
            "key-file" => "encrypted, key in a 0600 file",
            _ => "none stored",
        }
    }

    /// Mailboxes are off for this user (the admin's switch or an old override).
    pub fn mailboxes_off(&self) -> bool {
        self.email_available == Some(false) || !self.admin_enabled
    }

    /// Why the Agent email tools switch is unavailable (None = it can be
    /// switched): the gateway's reason, else the same order the gateway
    /// uses — mailboxes off, agent tools off, no mailbox, not in use.
    pub fn agent_tools_unavailable(&self) -> Option<String> {
        if !self.agent_tools_unavailable_reason.is_empty() {
            return Some(self.agent_tools_unavailable_reason.clone());
        }
        if self.mailboxes_off() {
            Some(REASON_ADMIN_MAILBOXES_OFF.into())
        } else if !self.agent_tools_available && self.configured {
            Some(REASON_ADMIN_AGENT_TOOLS_OFF.into())
        } else if !self.configured {
            Some(REASON_CONNECT_MAILBOX.into())
        } else if !self.enabled {
            Some(REASON_MAILBOX_NOT_IN_USE.into())
        } else {
            None
        }
    }

    /// Why the two notification switches are unavailable (None = usable).
    pub fn notifications_unavailable(&self) -> Option<String> {
        if !self.notifications_unavailable_reason.is_empty() {
            return Some(self.notifications_unavailable_reason.clone());
        }
        if self.mailboxes_off() {
            Some(REASON_ADMIN_MAILBOXES_OFF.into())
        } else if !self.configured {
            Some(REASON_CONNECT_MAILBOX.into())
        } else if !self.enabled {
            Some(REASON_MAILBOX_NOT_IN_USE.into())
        } else {
            None
        }
    }

    /// The Email address field's value: `email_address` (the stored one,
    /// possibly empty); on an older gateway the registered address, else
    /// the mailbox's own.
    pub fn email_address(&self) -> String {
        if let Some(stored) = &self.stored_email_address {
            return stored.clone();
        }
        if self.registered_address.is_empty() {
            self.address.clone()
        } else {
            self.registered_address.clone()
        }
    }

    /// The sign-in method in words ("Google", "Microsoft", "password").
    pub fn method_text(&self) -> String {
        match self.auth_kind.as_str() {
            "" => String::new(),
            "password" => "IMAP".into(),
            k if k.contains("google") => "Google".into(),
            k if k.contains("microsoft") => "Microsoft".into(),
            other => other.to_string(),
        }
    }

    /// The connected state line: "Connected as me@x.com · Google · checked
    /// 2026-09-30 18:00" (or the recorded error).
    pub fn connected_text(&self) -> String {
        let mut out = format!("Connected as {}", self.address);
        let method = self.method_text();
        if !method.is_empty() {
            out.push_str(" · ");
            out.push_str(&method);
        }
        if let Some(err) = &self.last_error {
            out.push_str(" · needs action: ");
            out.push_str(&err.text());
        } else if !self.last_test.is_empty() {
            out.push_str(" · checked ");
            out.push_str(&short_time(&self.last_test));
        }
        out
    }

    /// `(id, available, reason)` of an OAuth provider tab.
    pub fn oauth_provider(&self, id: &str) -> Option<(bool, String)> {
        self.oauth_providers
            .iter()
            .find(|(p, _, _)| p == id)
            .map(|(_, a, r)| (*a, r.clone()))
    }

    pub fn usage_text(&self) -> String {
        match (self.per_hour, self.per_day) {
            (Some(_), Some(_)) => format!(
                "{} sent this hour, {} today",
                self.used_last_hour, self.used_last_day
            ),
            _ => String::new(),
        }
    }
}

/// The sign-in-by-email flow of the Connection screen (DESIGN §4).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Recovery {
    /// The gateway URL `available` was read for (a new URL asks again).
    pub checked_url: String,
    /// `GET /session/recovery` `available`; `None` = not read yet.
    pub available: Option<bool>,
    pub step: RecoveryStep,
    /// The last inline error (a refused code, an unreachable gateway).
    pub error: Option<String>,
    /// The token a redeemed code returned (the UI moves it into the token
    /// field and connects, then clears it).
    pub new_token: Option<String>,
    /// The user the token belongs to (said once after sign-in).
    pub signed_in_user: String,
    /// The status line said "Signed in with a new token" after the
    /// connection with it was verified.
    pub announced: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub enum RecoveryStep {
    /// The link "Forgot your token? Email me a sign-in code".
    #[default]
    Idle,
    /// The request is in flight ("Sending…").
    Sending,
    /// The code step: the gateway's honest answer and the resend clock.
    Code(RecoveryAnswer),
    /// The code is being checked ("Checking the code…").
    Redeeming(RecoveryAnswer),
}

/// `POST /session/recovery/request`'s answer (DESIGN §4.1).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RecoveryAnswer {
    pub user_id: String,
    pub sent: bool,
    pub to: String,
    pub message: String,
    pub reason_code: String,
    pub retry_after_s: Option<i64>,
    /// When the answer landed (unix ms): the 30 s resend cooldown counts
    /// from here.
    pub at_ms: u64,
}

/// The status line after a redeemed code (the terminal console signs in
/// with a new token: `reset_token`, decision 2026-09-30).
pub const SIGNED_IN_NEW_TOKEN: &str = "Signed in with a new token — your old token no longer works. This console keeps it in memory; launch with --token <token> next time.";
/// The code step's extra line (only this console: the code rotates the token).
pub const CODE_GIVES_NEW_TOKEN: &str = "The code signs you in with a new token.";

/// Seconds a "Send a new code" waits after a request (DESIGN §4).
pub const RESEND_COOLDOWN_S: u64 = 30;

impl RecoveryAnswer {
    pub fn from_value(user_id: &str, v: &Value, at_ms: u64) -> RecoveryAnswer {
        let sent = b(v, "sent").unwrap_or(false);
        let mut message = s(v, "message");
        if message.is_empty() {
            // A gateway older than DESIGN §4.1 answers the same words for
            // every account: say exactly that much.
            message = "If this account has an email address, a sign-in code is on its way. It expires in 10 minutes.".into();
        }
        RecoveryAnswer {
            user_id: user_id.to_string(),
            sent: sent || v.get("sent").is_none(),
            to: s(v, "to"),
            message,
            reason_code: s(v, "reason_code"),
            retry_after_s: n(v, "retry_after_s"),
            at_ms,
        }
    }

    /// Seconds left before "Send a new code" works again (0 = now): the
    /// 30 s cooldown, or the gateway's `retry_after_s` when longer.
    pub fn resend_wait_s(&self, now_ms: u64) -> u64 {
        let wait = self
            .retry_after_s
            .map(|r| r.max(0) as u64)
            .unwrap_or(0)
            .max(RESEND_COOLDOWN_S);
        let elapsed = now_ms.saturating_sub(self.at_ms) / 1000;
        wait.saturating_sub(elapsed)
    }
}

/// An 8-digit code (DESIGN §4: "Use code" stays unavailable until then).
pub fn code_complete(code: &str) -> bool {
    let c = code.trim();
    c.len() == 8 && c.chars().all(|ch| ch.is_ascii_digit())
}

/// "2026-09-30T18:00:00+00:00" → "2026-09-30 18:00" (a timestamp in a
/// status line; anything else unchanged).
pub fn short_time(ts: &str) -> String {
    if ts.len() >= 16 && ts.as_bytes().get(10) == Some(&b'T') {
        format!("{} {}", &ts[..10], &ts[11..16])
    } else {
        ts.to_string()
    }
}

/// One notification event row.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct NotifyEvent {
    pub id: String,
    pub label: String,
    pub email: bool,
}

/// `GET /me/notifications`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MyNotifications {
    pub events: Vec<NotifyEvent>,
    pub available: bool,
    pub to: String,
    pub unavailable_reason: String,
    pub outbox_text: String,
}

/// " 3 held by your send limit until 14:05." from the outbox's
/// `rate_limited {count, cause, resets_at}` (local time); "" when none.
fn held_text(v: Option<&Value>) -> String {
    let Some(r) = v.filter(|r| r.is_object()) else {
        return String::new();
    };
    let count = r.get("count").and_then(Value::as_u64).unwrap_or(0);
    if count == 0 {
        return String::new();
    }
    match r
        .get("resets_at")
        .and_then(Value::as_str)
        .and_then(crate::localtime::local_parts)
    {
        Some((_, hm)) => format!(" {count} held by your send limit until {hm}."),
        None => format!(" {count} held by your send limit."),
    }
}

impl MyNotifications {
    pub fn from_value(v: &Value) -> MyNotifications {
        let email = v
            .get("channels")
            .and_then(|c| c.get("email"))
            .cloned()
            .unwrap_or(Value::Null);
        let ob = v.get("outbox").cloned().unwrap_or(Value::Null);
        let fail = MailError::from_value(ob.get("last_failure"))
            .map(|e| format!(" Last failure: {}", e.text()))
            .unwrap_or_default();
        MyNotifications {
            events: v
                .get("events")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|e| NotifyEvent {
                            id: s(e, "id"),
                            label: s(e, "label"),
                            email: b(e, "email").unwrap_or(false),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            available: b(&email, "available").unwrap_or(false),
            to: s(&email, "to"),
            unavailable_reason: s(v, "unavailable_reason"),
            outbox_text: format!(
                "{} sent, {} waiting, {} failed.{fail}{held}",
                n(&ob, "sent").unwrap_or(0),
                n(&ob, "queued").unwrap_or(0),
                n(&ob, "failed").unwrap_or(0),
                held = held_text(ob.get("rate_limited")),
            ),
        }
    }

    /// `(job_failed, approval_needed)` read from the events list of a
    /// gateway that predates the two-switch model (`job_failed` OR
    /// `automation_failed`, as the gateway's own migration maps them).
    pub fn two_switches(&self) -> (bool, bool) {
        let on = |id: &str| self.events.iter().any(|e| e.id == id && e.email);
        (
            on("job_failed") || on("automation_failed"),
            on("approval_needed"),
        )
    }

    pub fn channel_text(&self) -> String {
        if self.available {
            format!(
                "Notifications are emailed to {}, sent by your own account (your recipient policy and send limits apply).",
                self.to
            )
        } else if self.unavailable_reason.is_empty() {
            "Email notifications need a connected, turned-on email account.".into()
        } else {
            self.unavailable_reason.clone()
        }
    }
}

/// `GET /admin/email/capabilities` — the gateway-wide defaults (admin).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EmailCaps {
    pub email: bool,
    pub agent_tools: bool,
    pub recovery: bool,
}

impl EmailCaps {
    pub fn from_value(v: &Value) -> EmailCaps {
        let mut out = EmailCaps::default();
        for c in v
            .get("capabilities")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let on = b(c, "default").unwrap_or(false);
            match s(c, "id").as_str() {
                "email" => out.email = on,
                "email_agent_tools" => out.agent_tools = on,
                "email_recovery" => out.recovery = on,
                _ => {}
            }
        }
        out
    }

    pub fn body(&self) -> Value {
        json!({"email": self.email, "email_agent_tools": self.agent_tools, "email_recovery": self.recovery})
    }
}

/// The status sentence of an admin email switch (`PUT
/// /admin/email/capabilities` with one key): the NEW state, in the words
/// of DESIGN §5.2.
pub fn caps_state_text(body: &Value) -> String {
    let mut out: Vec<&str> = Vec::new();
    let on = |k: &str| body.get(k).and_then(Value::as_bool);
    match on("email") {
        Some(true) => out.push("Mailboxes are on for all users."),
        Some(false) => out.push("Mailboxes are off for all users."),
        None => {}
    }
    match on("email_agent_tools") {
        Some(true) => {
            out.push("Agent email tools are available to users (each user still opts in).")
        }
        Some(false) => out.push("Agent email tools are off for all users."),
        None => {}
    }
    match on("email_recovery") {
        Some(true) => out.push("Sign-in by email is on."),
        Some(false) => out.push("Sign-in by email is off."),
        None => {}
    }
    if out.is_empty() {
        "Email defaults saved.".into()
    } else {
        out.join(" ")
    }
}

/// The admin Users table's Mailbox cell (`/admin/users` rows carry
/// `email_account: {configured, address, state, admin_enabled,
/// capabilities: {email, email_agent_tools: {value, source}}}`): the
/// connection in words — "connected as me@x.com", "not connected" —
/// whether mailboxes are off for this user (`admin_enabled: false`), and
/// the per-user override read from `capabilities` (a `user` source with
/// value false; `None` when the gateway does not send `capabilities`).
pub fn mailbox_cell(user: &Value) -> (String, bool, Option<bool>) {
    let Some(acc) = user.get("email_account").filter(|a| a.is_object()) else {
        return ("—".into(), false, None);
    };
    let state = s(acc, "state");
    let address = s(acc, "address");
    let configured = b(acc, "configured").unwrap_or(false);
    let not_allowed = b(acc, "admin_enabled") == Some(false);
    let conn = if !configured {
        if state == "unknown" {
            "unknown".to_string()
        } else {
            "not connected".to_string()
        }
    } else if address.is_empty() {
        "connected".to_string()
    } else if state == "needs action" {
        format!("needs action · {address}")
    } else if state == "turned off by the user" {
        format!("connected as {address} · not in use")
    } else {
        format!("connected as {address}")
    };
    let user_off = |cap: &str| {
        acc.get("capabilities")
            .and_then(|c| c.get(cap))
            .map(|c| s(c, "source") == "user" && b(c, "value") == Some(false))
    };
    let flag = match (user_off("email"), user_off("email_agent_tools")) {
        (None, None) => None,
        (e, t) => Some(e.unwrap_or(false) || t.unwrap_or(false)),
    };
    (conn, not_allowed, flag)
}

/// The cell as shown, with the note. A per-user override (the old UI's,
/// or the capabilities migration's pin) reads "not allowed for this user"
/// — or "agent email tools not allowed for this user" when only the tools
/// are pinned off — and `x` resets it. Mailboxes off without an override
/// (the admin's "Mailboxes for users" switch) reads "mailboxes off":
/// nothing to reset. `override_flag` is the gateway's (`None`: a gateway
/// that does not say — no Reset is offered). Returns `(text, resettable)`.
pub fn mailbox_cell_text(
    conn: &str,
    not_allowed: bool,
    override_flag: Option<bool>,
) -> (String, bool) {
    match (override_flag == Some(true), not_allowed) {
        (true, true) => (format!("{conn} · not allowed for this user"), true),
        (true, false) => (
            format!("{conn} · agent email tools not allowed for this user"),
            true,
        ),
        (false, true) => (format!("{conn} · mailboxes off"), false),
        (false, false) => (conn.to_string(), false),
    }
}

/// `POST /me/email/discover` (CONTRACT §5.3): the mail servers found for an
/// address, by a deterministic lookup (known providers, autoconfig, ISPDB,
/// SRV, MX). Unknown fields are ignored; a missing field reads empty.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Discovery {
    pub address: String,
    pub domain: String,
    pub found: bool,
    pub source: String,
    pub provider: String,
    pub imap: Option<MailServer>,
    pub smtp: Option<MailServer>,
    pub username: String,
    /// `defaults` (DESIGN-v2 §6): the server fields the IMAP pane pre-fills.
    /// `None` = the gateway sent no `defaults` (the page says so; there is
    /// no fallback to the older top-level fields).
    pub defaults: Option<ServerDefaults>,
}

/// `defaults` of `POST /me/email/discover` (DESIGN-v2 §6): core's
/// `server_defaults(address)` — the discovered servers + login form, else
/// the standard `imap./smtp.<domain>` 993/465 SSL, with one sentence.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ServerDefaults {
    pub imap: MailServer,
    pub smtp: MailServer,
    pub login: String,
    /// "discovered" | "standard".
    pub source: String,
    pub provider: String,
    pub message: String,
}

impl ServerDefaults {
    pub fn from_value(v: Option<&Value>) -> Option<ServerDefaults> {
        let v = v.filter(|v| v.is_object())?;
        Some(ServerDefaults {
            imap: MailServer::from_value(v.get("imap"))?,
            smtp: MailServer::from_value(v.get("smtp"))?,
            login: s(v, "login"),
            source: s(v, "source"),
            provider: s(v, "provider"),
            message: s(v, "message"),
        })
    }

    /// The values the pane shows the moment the address has a domain,
    /// before discovery answers: imap.<domain> 993 SSL, smtp.<domain> 465
    /// SSL, the address as the login (core's standard fallback).
    pub fn standard(address: &str) -> Option<ServerDefaults> {
        let domain = address_domain(address)?;
        Some(ServerDefaults {
            imap: MailServer {
                host: format!("imap.{domain}"),
                port: Some(993),
                security: "ssl".into(),
                folder: String::new(),
            },
            smtp: MailServer {
                host: format!("smtp.{domain}"),
                port: Some(465),
                security: "ssl".into(),
                folder: String::new(),
            },
            login: address.trim().to_string(),
            source: "standard".into(),
            provider: String::new(),
            message: format!(
                "Standard settings for {domain} \u{2014} change them if your provider uses others."
            ),
        })
    }
}

/// The line shown when a discovery answer carries no `defaults` (a gateway
/// older than DESIGN-v2 §6): loud, never a silent fallback.
pub const DISCOVERY_NO_DEFAULTS: &str = "The gateway's server lookup sent no settings to pre-fill (it predates this console). Check the servers below.";

impl Discovery {
    pub fn from_value(v: &Value) -> Discovery {
        Discovery {
            address: s(v, "address"),
            domain: s(v, "domain"),
            found: b(v, "found").unwrap_or(false),
            source: s(v, "source"),
            provider: s(v, "provider"),
            imap: MailServer::from_value(v.get("imap")),
            smtp: MailServer::from_value(v.get("smtp")),
            username: s(v, "username"),
            defaults: ServerDefaults::from_value(v.get("defaults")),
        }
    }

    /// "imap.fastmail.com · 993 · SSL  ·  smtp.fastmail.com · 465 · SSL"
    /// (DESIGN §6); `None` when discovery found nothing to show.
    pub fn summary(&self) -> Option<String> {
        if !self.found {
            return None;
        }
        let leg = |m: &MailServer| {
            let mut out = m.host.clone();
            if let Some(p) = m.port {
                out.push_str(&format!(" · {p}"));
            }
            if !m.security.is_empty() {
                out.push_str(&format!(" · {}", security_label(&m.security)));
            }
            out
        };
        let parts: Vec<String> = [self.imap.as_ref(), self.smtp.as_ref()]
            .into_iter()
            .flatten()
            .map(leg)
            .collect();
        if parts.is_empty() {
            None
        } else {
            Some(parts.join("  ·  "))
        }
    }

    /// The sentence shown when nothing was found (Server settings open by
    /// themselves).
    pub fn not_found_text(&self) -> String {
        let domain = if self.domain.is_empty() {
            self.address
                .rsplit_once('@')
                .map(|(_, d)| d.to_string())
                .unwrap_or_default()
        } else {
            self.domain.clone()
        };
        format!("Couldn't find the mail servers for {domain}. Enter them here.")
    }
}

/// "ssl" → "SSL", "starttls" → "STARTTLS".
pub fn security_label(sec: &str) -> String {
    sec.to_uppercase()
}

/// The domain of an address worth asking discovery about (`None` for a
/// half-typed address).
pub fn address_domain(address: &str) -> Option<String> {
    let a = address.trim();
    let (local, domain) = a.rsplit_once('@')?;
    if local.is_empty() || !domain.contains('.') || domain.ends_with('.') {
        return None;
    }
    Some(domain.to_lowercase())
}

/// The IMAP pane's server fields (always visible, pre-filled).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ServerSettings {
    pub imap_host: String,
    pub imap_port: String,
    pub imap_security: String,
    pub smtp_host: String,
    pub smtp_port: String,
    pub smtp_security: String,
}

/// The IMAP pane's ONE Connect (`PUT /me/email`, save + test): the address,
/// the password and the servers shown (an empty host leaves that leg to the
/// gateway's discovery). `login` only when the person revealed "My provider
/// uses a different login name" and changed it; never a display name (the
/// gateway keeps a stored one or defaults it, DESIGN-v2 §3).
pub fn imap_connect_body(
    address: &str,
    password: &str,
    sv: &ServerSettings,
    login: Option<&str>,
) -> Result<Value, String> {
    if address.trim().is_empty() {
        return Err("Type the email address of the mailbox.".into());
    }
    if password.is_empty() {
        return Err("Type the password (an app password if your provider needs one).".into());
    }
    let mut body = json!({"address": address.trim(), "password": password, "test": true});
    if !sv.imap_host.trim().is_empty() {
        body["imap"] = json!({
            "host": sv.imap_host.trim(),
            "port": port_value(&sv.imap_port, "IMAP")?,
            "security": if sv.imap_security.is_empty() { "ssl" } else { sv.imap_security.as_str() },
        });
    }
    if !sv.smtp_host.trim().is_empty() {
        body["smtp"] = json!({
            "host": sv.smtp_host.trim(),
            "port": port_value(&sv.smtp_port, "SMTP")?,
            "security": if sv.smtp_security.is_empty() { "ssl" } else { sv.smtp_security.as_str() },
        });
    }
    if let Some(l) = login.map(str::trim).filter(|l| !l.is_empty()) {
        body["username"] = json!(l);
    }
    Ok(body)
}

/// The sentence "Send a test" shows (DESIGN-v2 §3/§6
/// `POST /me/notifications/test`): the API's `message`, always — `Ok` when
/// it was sent, `Err` (the reason) when not. Never a bare state name.
pub fn test_notification_outcome(
    write: &Result<Value, crate::api::ApiError>,
) -> Result<String, String> {
    match write {
        Ok(v) => {
            let sent = b(v, "sent").unwrap_or(false);
            // A send-limit refusal is said from its typed fields so the reset
            // time is the VIEWER's local time (the gateway's sentence carries
            // the gateway machine's clock).
            if !sent && s(v, "reason_code") == "rate_limited" {
                if let Some(text) = rate_limited_text(v.get("limit")) {
                    return Err(text);
                }
            }
            let message = s(v, "message");
            match (sent, message.is_empty()) {
                (true, false) => Ok(message),
                (true, true) => Ok("Sent.".into()),
                (false, false) => Err(message),
                (false, true) => Err(
                    "Not sent, and the gateway gave no reason (it predates this console).".into(),
                ),
            }
        }
        Err(e) => Err(email_error_text(e)),
    }
}

/// "Not sent: hourly limit reached (20 of 20 this hour) — resets at 14:05."
/// from `limit {window, limit, used, resets_at}`, the reset in local time.
/// None when the gateway sent no usable `limit` (its own sentence is used).
pub fn rate_limited_text(limit: Option<&Value>) -> Option<String> {
    let l = limit?;
    let window = l.get("window").and_then(Value::as_str)?;
    let adj = match window {
        "hour" => "hourly",
        "day" => "daily",
        _ => return None,
    };
    let max = l.get("limit").and_then(Value::as_u64)?;
    let used = l.get("used").and_then(Value::as_u64)?;
    let resets = l.get("resets_at").and_then(Value::as_str)?;
    let hm = crate::localtime::local_parts(resets)?.1;
    Some(format!(
        "Not sent: {adj} limit reached ({used} of {max} this {window}) \u{2014} resets at {hm}."
    ))
}

/// `PUT /me/email/notifications` — one switch at a time.
pub fn notification_switch_body(key: &str, on: bool) -> Value {
    let mut m = serde_json::Map::new();
    m.insert(key.to_string(), Value::Bool(on));
    Value::Object(m)
}

fn port_value(raw: &str, label: &str) -> Result<Value, String> {
    let t = raw.trim();
    if t.is_empty() {
        return Ok(Value::Null);
    }
    t.parse::<u16>()
        .map(|p| json!(p))
        .map_err(|_| format!("the {label} port must be a number (1-65535)"))
}

/// The connect body (`PUT /me/email`): the web form's fields, same shape.
#[allow(clippy::too_many_arguments)]
pub fn connect_body(
    address: &str,
    display_name: &str,
    username: &str,
    password: &str,
    imap: (&str, &str, &str, &str),
    smtp: (&str, &str, &str),
) -> Result<Value, String> {
    if address.trim().is_empty() {
        return Err("the address is required".into());
    }
    if password.is_empty() {
        return Err(
            "give the password (or app password): it is never shown again once stored".into(),
        );
    }
    let (ih, ip, isec, ifolder) = imap;
    let (sh, sp, ssec) = smtp;
    let imap_v = if ih.trim().is_empty() {
        Value::Null
    } else {
        json!({
            "host": ih.trim(),
            "port": port_value(ip, "IMAP")?,
            "security": if isec.is_empty() { "ssl" } else { isec },
            "folder": if ifolder.trim().is_empty() { "INBOX" } else { ifolder.trim() },
        })
    };
    let smtp_v = if sh.trim().is_empty() {
        Value::Null
    } else {
        json!({
            "host": sh.trim(),
            "port": port_value(sp, "SMTP")?,
            "security": if ssec.is_empty() { "ssl" } else { ssec },
        })
    };
    if imap_v.is_null() && smtp_v.is_null() {
        return Err(
            "give at least the IMAP host (to read mail) or the SMTP host (to send mail)".into(),
        );
    }
    Ok(json!({
        "address": address.trim(),
        "display_name": display_name.trim(),
        "username": username.trim(),
        "password": password,
        "imap": imap_v,
        "smtp": smtp_v,
        "test": true,
    }))
}

/// `PUT /me/email/policy`: one address or domain per line.
pub fn policy_body(mode: &str, entries: &str) -> Value {
    let list: Vec<String> = entries
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect();
    json!({"mode": if mode.is_empty() { "allowlist" } else { mode }, "entries": list})
}

/// `PUT /me/email/limits`.
pub fn limits_body(per_hour: &str, per_day: &str) -> Result<Value, String> {
    let parse = |raw: &str, label: &str| -> Result<Value, String> {
        let t = raw.trim();
        if t.is_empty() {
            return Ok(Value::Null);
        }
        t.parse::<u32>()
            .map(|v| json!(v))
            .map_err(|_| format!("the {label} limit must be a whole number"))
    };
    Ok(json!({"per_hour": parse(per_hour, "per-hour")?, "per_day": parse(per_day, "per-day")?}))
}

/// `PUT /me/notifications`.
pub fn notifications_body(events: &[NotifyEvent]) -> Value {
    let mut email = serde_json::Map::new();
    for e in events {
        email.insert(e.id.clone(), Value::Bool(e.email));
    }
    json!({ "email": Value::Object(email) })
}

/// A typed email refusal as the gateway words it (`detail: {cause, fix}`),
/// else the error's own text.
pub fn email_error_text(e: &crate::api::ApiError) -> String {
    if let Some(body) = &e.body {
        // A connect that names its failing step ("Sign-in refused by
        // imap.x.com — check the password.") or a discovery that found
        // nothing: the gateway's one sentence is the whole answer.
        let msg = s(body, "message");
        let named =
            body.get("step").is_some() || s(body, "reason_code") == "email_discovery_failed";
        if named && !msg.is_empty() {
            return msg;
        }
        let cause = s(body, "cause");
        if !cause.is_empty() {
            let fix = s(body, "fix");
            return if fix.is_empty() {
                cause
            } else {
                format!("{cause} Fix: {fix}")
            };
        }
        let msg = s(body, "message");
        if !msg.is_empty() {
            return msg;
        }
    }
    e.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_gateway_status_and_words_the_state() {
        let e = MyEmail::from_value(&json!({
            "schema": "email_settings_v1", "configured": true, "enabled": true,
            "admin_enabled": true, "effective_enabled": true,
            "address": "me@example.test", "username": "me@example.test", "auth_kind": "password",
            "imap": {"host": "imap.example.test", "port": 993, "security": "ssl", "folder": "INBOX"},
            "smtp": {"host": "smtp.example.test", "port": 587, "security": "starttls"},
            "secret_storage": "os-keychain",
            "policy": {"mode": "allowlist", "entries": ["me@example.test"]},
            "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 1, "used_last_day": 3},
            "status": {"last_test": "t", "legs": {"imap": {"ok": true}, "smtp": {"ok": false, "code": "email_auth_failed", "cause": "rejected"}},
                       "last_error": {"code": "email_auth_failed", "cause": "rejected", "fix": "update it"}},
            "watcher": {"state": "watching", "last_poll": "now"},
            "agent_tools": {"enabled": true, "available": true, "active": false, "reason": "no connected, turned-on email account"},
            "store": {"kind": "gateway", "label": "Your email account on this gateway."}
        }));
        assert!(e.agent_tools_enabled && !e.agent_tools_active);
        assert!(e.agent_tools_available);
        assert_eq!(e.store_label, "Your email account on this gateway.");
        let caps = EmailCaps::from_value(&json!({"capabilities": [
            {"id": "email", "default": true}, {"id": "email_agent_tools", "default": false}, {"id": "email_recovery", "default": true}
        ]}));
        assert_eq!(
            caps,
            EmailCaps {
                email: true,
                agent_tools: false,
                recovery: true
            }
        );
        assert_eq!(
            caps.body(),
            json!({"email": true, "email_agent_tools": false, "email_recovery": true})
        );
        assert_eq!(
            e.agent_tools_text(),
            "off — no connected, turned-on email account"
        );
        assert_eq!(e.state_label(), "needs action");
        assert_eq!(e.imap.as_ref().unwrap().text(), "imap.example.test:993 ssl");
        assert_eq!(e.smtp_test, "failed: rejected");
        assert_eq!(
            e.last_error.as_ref().unwrap().text(),
            "rejected Fix: update it"
        );
        assert_eq!(e.policy_entries, vec!["me@example.test".to_string()]);
        assert_eq!(e.usage_text(), "1 sent this hour, 3 today");
        assert_eq!(e.credentials_text(), "encrypted, key in the OS keychain");
        let off = MyEmail::from_value(
            &json!({"configured": true, "enabled": true, "admin_enabled": false}),
        );
        assert_eq!(off.state_label(), "turned off by an administrator");
        assert_eq!(
            MyEmail::from_value(&json!({})).state_label(),
            "not connected"
        );
    }

    #[test]
    fn builds_the_web_bodies() {
        let body = connect_body(
            " me@example.test ",
            "",
            "",
            "pw",
            ("imap.example.test", "993", "ssl", ""),
            ("smtp.example.test", "", "starttls"),
        )
        .unwrap();
        assert_eq!(
            body["imap"],
            json!({"host": "imap.example.test", "port": 993, "security": "ssl", "folder": "INBOX"})
        );
        assert_eq!(body["smtp"]["port"], Value::Null);
        assert_eq!(body["address"], json!("me@example.test"));
        assert!(connect_body("a@b.test", "", "", "", ("h", "", "ssl", ""), ("", "", "")).is_err());
        assert!(connect_body(
            "a@b.test",
            "",
            "",
            "pw",
            ("h", "x", "ssl", ""),
            ("", "", "")
        )
        .is_err());
        assert_eq!(
            policy_body("denylist", " a@b.test \n\nexample.org"),
            json!({"mode": "denylist", "entries": ["a@b.test", "example.org"]})
        );
        assert_eq!(
            limits_body("5", "").unwrap(),
            json!({"per_hour": 5, "per_day": null})
        );
        assert!(limits_body("-1", "").is_err());
        let evs = vec![NotifyEvent {
            id: "job_failed".into(),
            label: "x".into(),
            email: true,
        }];
        assert_eq!(
            notifications_body(&evs),
            json!({"email": {"job_failed": true}})
        );
    }

    #[test]
    fn mailbox_cell_reads_the_admin_row() {
        assert_eq!(
            mailbox_cell(
                &json!({"email_account": {"configured": true, "address": "a@x.io", "state": "connected", "admin_enabled": true}})
            ),
            ("connected as a@x.io".to_string(), false, None)
        );
        assert_eq!(
            mailbox_cell(
                &json!({"email_account": {"configured": false, "state": "not connected", "admin_enabled": true}})
            ),
            ("not connected".to_string(), false, None)
        );
        let (conn, off, flag) = mailbox_cell(
            &json!({"email_account": {"configured": true, "address": "b@x.io", "state": "turned off by an administrator", "admin_enabled": false,
                "capabilities": {"email": {"value": false, "source": "user"}, "email_agent_tools": {"value": true, "source": "gateway"}}}}),
        );
        assert_eq!(
            (conn.as_str(), off, flag),
            ("connected as b@x.io", true, Some(true))
        );
        assert_eq!(
            mailbox_cell_text(&conn, off, flag),
            (
                "connected as b@x.io · not allowed for this user".to_string(),
                true
            )
        );
        // Mailboxes off for everyone (a gateway source): nothing to reset.
        let (_, off, flag) = mailbox_cell(
            &json!({"email_account": {"configured": false, "state": "x", "admin_enabled": false,
                "capabilities": {"email": {"value": false, "source": "gateway"}, "email_agent_tools": {"value": true, "source": "built-in"}}}}),
        );
        assert_eq!(flag, Some(false));
        assert_eq!(
            mailbox_cell_text("not connected", off, flag),
            ("not connected · mailboxes off".to_string(), false)
        );
        // Only the tools pinned off (the migration's pin).
        let (_, off, flag) = mailbox_cell(
            &json!({"email_account": {"configured": false, "state": "x", "admin_enabled": true,
                "capabilities": {"email": {"value": true, "source": "gateway"}, "email_agent_tools": {"value": false, "source": "user"}}}}),
        );
        assert_eq!(
            mailbox_cell_text("not connected", off, flag),
            (
                "not connected · agent email tools not allowed for this user".to_string(),
                true
            )
        );
        // A gateway that does not send capabilities: no Reset offered.
        assert_eq!(
            mailbox_cell_text("c", true, None),
            ("c · mailboxes off".to_string(), false)
        );
        assert_eq!(mailbox_cell(&json!({})), ("—".to_string(), false, None));
    }

    #[test]
    fn the_account_page_reads_the_new_fields_and_tolerates_old_gateways() {
        let e = MyEmail::from_value(&json!({
            "configured": false, "address": "", "registered_address": "box@x.io", "email_address": ""
        }));
        assert_eq!(e.email_address(), "", "email_address wins, even empty");
        let old = MyEmail::from_value(&json!({"configured": true, "address": "box@x.io"}));
        assert_eq!(old.email_address(), "box@x.io");
        assert_eq!(old.notify_job_failed, None);
        assert_eq!(
            MyEmail::from_value(&json!({"configured": false}))
                .agent_tools_unavailable()
                .as_deref(),
            Some(REASON_CONNECT_MAILBOX)
        );
        assert_eq!(
            MyEmail::from_value(
                &json!({"configured": true, "enabled": true, "email_available": false})
            )
            .notifications_unavailable()
            .as_deref(),
            Some(REASON_ADMIN_MAILBOXES_OFF)
        );
    }

    #[test]
    fn discovery_summary_and_the_one_connect_body() {
        let d = Discovery::from_value(&json!({
            "address": "me@fastmail.test", "domain": "fastmail.test", "found": true,
            "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
            "smtp": {"host": "smtp.fastmail.com", "port": 465, "security": "ssl"}
        }));
        assert_eq!(
            d.summary().as_deref(),
            Some("imap.fastmail.com · 993 · SSL  ·  smtp.fastmail.com · 465 · SSL")
        );
        let none = Discovery::from_value(&json!({"address": "me@nowhere.test", "found": false}));
        assert_eq!(none.summary(), None);
        assert_eq!(
            none.not_found_text(),
            "Couldn't find the mail servers for nowhere.test. Enter them here."
        );
        assert_eq!(address_domain("me@x.io").as_deref(), Some("x.io"));
        assert_eq!(address_domain("me@x"), None);
        let empty = ServerSettings::default();
        let body = imap_connect_body(" me@x.io ", "pw", &empty, None).unwrap();
        assert_eq!(
            body,
            json!({"address": "me@x.io", "password": "pw", "test": true})
        );
        assert!(imap_connect_body("me@x.io", "", &empty, None).is_err());
        let sv = ServerSettings {
            imap_host: "imap.x.io".into(),
            imap_port: "993".into(),
            smtp_host: "smtp.x.io".into(),
            smtp_port: "465".into(),
            ..ServerSettings::default()
        };
        let body = imap_connect_body("me@x.io", "pw", &sv, None).unwrap();
        assert_eq!(body["imap"]["host"], json!("imap.x.io"));
        assert_eq!(body["smtp"]["port"], json!(465));
        assert!(body.get("username").is_none());
        assert!(body.get("display_name").is_none());
        let body = imap_connect_body("me@x.io", "pw", &sv, Some(" me ")).unwrap();
        assert_eq!(body["username"], json!("me"));
        assert_eq!(
            notification_switch_body("job_failed", false),
            json!({"job_failed": false})
        );
    }

    #[test]
    fn recovery_answers_codes_and_admin_state_texts() {
        let legacy = RecoveryAnswer::from_value("a", &json!({"ok": true}), 0);
        assert!(
            legacy.sent,
            "an old constant answer still opens the code step"
        );
        assert!(legacy
            .message
            .starts_with("If this account has an email address"));
        let a = RecoveryAnswer::from_value("a", &json!({"sent": true, "message": "m"}), 10_000);
        assert_eq!(a.resend_wait_s(10_000), 30);
        assert_eq!(a.resend_wait_s(25_000), 15);
        assert_eq!(a.resend_wait_s(41_000), 0);
        assert!(code_complete(" 12345678 "));
        assert!(!code_complete("1234567"));
        assert!(!code_complete("1234567a"));
        assert_eq!(
            caps_state_text(&json!({"email": true})),
            "Mailboxes are on for all users."
        );
        assert_eq!(
            caps_state_text(&json!({"email_recovery": false})),
            "Sign-in by email is off."
        );
    }

    #[test]
    fn a_failed_connect_says_the_step_in_the_gateways_words() {
        let e = crate::api::ApiError {
            kind: crate::api::ApiErrorKind::Http(422),
            message: "x".into(),
            body: Some(json!({"reason_code": "email_auth_failed", "step": "imap",
                "message": "Sign-in refused by imap.x.com — check the password.",
                "cause": "The server refused the sign-in.", "fix": "Check the password."})),
            timed_out: false,
        };
        assert_eq!(
            email_error_text(&e),
            "Sign-in refused by imap.x.com — check the password."
        );
    }

    #[test]
    fn discovery_defaults_are_read_and_the_standard_ones_fill_first() {
        let d = Discovery::from_value(&json!({
            "address": "me@fastmail.test", "found": true,
            "defaults": {
                "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.fastmail.com", "port": 587, "security": "starttls"},
                "login": "me@fastmail.test", "source": "discovered", "provider": "Fastmail",
                "message": "Settings found for fastmail.test."
            }
        }));
        let def = d.defaults.expect("defaults");
        assert_eq!(def.smtp.port, Some(587));
        assert_eq!(def.smtp.security, "starttls");
        assert_eq!(def.message, "Settings found for fastmail.test.");
        // A reply without `defaults` reads None: the page says so.
        let old = Discovery::from_value(&json!({"address": "me@x.io", "found": true,
            "imap": {"host": "imap.x.io", "port": 993, "security": "ssl"}}));
        assert_eq!(old.defaults, None);
        let st = ServerDefaults::standard("me@example.com").expect("standard");
        assert_eq!(
            (
                st.imap.host.as_str(),
                st.imap.port,
                st.imap.security.as_str()
            ),
            ("imap.example.com", Some(993), "ssl")
        );
        assert_eq!(
            (
                st.smtp.host.as_str(),
                st.smtp.port,
                st.smtp.security.as_str()
            ),
            ("smtp.example.com", Some(465), "ssl")
        );
        assert_eq!(st.login, "me@example.com");
        assert!(ServerDefaults::standard("me@example").is_none());
    }

    #[test]
    fn a_test_notification_says_the_apis_sentence() {
        let limited = json!({"ok": true, "sent": false, "reason_code": "rate_limited",
            "message": "Not sent: hourly limit reached (20 of 20 this hour) \u{2014} resets at 14:05.",
            "limit": {"window": "hour", "limit": 20, "used": 20, "resets_at": "2026-10-01T14:05:00Z"}});
        // The reset time is the VIEWER's local time, from the typed fields.
        let local = crate::localtime::local_hm("2026-10-01T14:05:00Z");
        assert_eq!(
            test_notification_outcome(&Ok(limited)),
            Err(format!(
                "Not sent: hourly limit reached (20 of 20 this hour) \u{2014} resets at {local}."
            ))
        );
        // Without a usable `limit`, the gateway's own sentence.
        let bare_limit = json!({"ok": true, "sent": false, "reason_code": "rate_limited",
            "message": "Not sent: limit reached.", "limit": null});
        assert_eq!(
            test_notification_outcome(&Ok(bare_limit)),
            Err("Not sent: limit reached.".into())
        );
        // The outbox's held rows say when they go, in local time.
        let n = MyNotifications::from_value(
            &json!({"outbox": {"sent": 1, "queued": 3, "failed": 0,
            "rate_limited": {"count": 3, "cause": "hourly limit", "resets_at": "2026-10-01T14:05:00Z"}}}),
        );
        assert!(
            n.outbox_text
                .ends_with(&format!(" 3 held by your send limit until {local}.")),
            "{}",
            n.outbox_text
        );
        let sent =
            json!({"ok": true, "sent": true, "reason_code": null, "message": "Sent to a@b.test."});
        assert_eq!(
            test_notification_outcome(&Ok(sent)),
            Ok("Sent to a@b.test.".into())
        );
        // No message: a sentence, never the state name.
        let bare = json!({"ok": true, "sent": false, "state": "queued"});
        let out = test_notification_outcome(&Ok(bare)).unwrap_err();
        assert!(!out.contains("queued"), "{out}");
    }
}
