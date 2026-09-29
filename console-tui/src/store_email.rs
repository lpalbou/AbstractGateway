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
}

fn leg_text(v: Option<&Value>) -> String {
    match v {
        None => "-".into(),
        Some(leg) => match leg.get("ok") {
            Some(Value::Bool(true)) => "ok".into(),
            Some(Value::Bool(false)) => {
                let cause = s(leg, "cause");
                format!(
                    "failed: {}",
                    if cause.is_empty() { s(leg, "code") } else { cause }
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
        let admin_disabled = v
            .get("admin_disabled")
            .map(|d| format!("{} {}", s(d, "cause"), s(d, "fix")).trim().to_string())
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
                .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
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
            notices: v
                .get("notices")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
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

    pub fn credentials_text(&self) -> &'static str {
        match self.secret_storage.as_str() {
            "os-keychain" => "encrypted, key in the OS keychain",
            "key-file" => "encrypted, key in a 0600 file",
            _ => "none stored",
        }
    }

    pub fn usage_text(&self) -> String {
        match (self.per_hour, self.per_day) {
            (Some(_), Some(_)) => format!(
                "{} sent in the last hour, {} in the last day",
                self.used_last_hour, self.used_last_day
            ),
            _ => String::new(),
        }
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
                "{} sent, {} waiting, {} failed.{fail}",
                n(&ob, "sent").unwrap_or(0),
                n(&ob, "queued").unwrap_or(0),
                n(&ob, "failed").unwrap_or(0)
            ),
        }
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

/// The admin Users table's mailbox cell (`/admin/users` rows carry
/// `email_account: {configured, address, state, admin_enabled}`).
pub fn mailbox_state(user: &Value) -> (String, bool) {
    match user.get("email_account") {
        Some(acc) if acc.is_object() => (
            {
                let st = s(acc, "state");
                if st.is_empty() {
                    "—".into()
                } else {
                    st
                }
            },
            b(acc, "admin_enabled").unwrap_or(true),
        ),
        _ => ("—".into(), true),
    }
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
        return Err("give the password (or app password): it is never shown again once stored".into());
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
        return Err("give at least the IMAP host (to read mail) or the SMTP host (to send mail)".into());
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
            "watcher": {"state": "watching", "last_poll": "now"}
        }));
        assert_eq!(e.state_label(), "needs action");
        assert_eq!(e.imap.as_ref().unwrap().text(), "imap.example.test:993 ssl");
        assert_eq!(e.smtp_test, "failed: rejected");
        assert_eq!(e.last_error.as_ref().unwrap().text(), "rejected Fix: update it");
        assert_eq!(e.policy_entries, vec!["me@example.test".to_string()]);
        assert_eq!(e.usage_text(), "1 sent in the last hour, 3 in the last day");
        assert_eq!(e.credentials_text(), "encrypted, key in the OS keychain");
        let off = MyEmail::from_value(&json!({"configured": true, "enabled": true, "admin_enabled": false}));
        assert_eq!(off.state_label(), "turned off by an administrator");
        assert_eq!(MyEmail::from_value(&json!({})).state_label(), "not connected");
    }

    #[test]
    fn builds_the_web_bodies() {
        let body = connect_body(
            " me@example.test ", "", "", "pw",
            ("imap.example.test", "993", "ssl", ""),
            ("smtp.example.test", "", "starttls"),
        )
        .unwrap();
        assert_eq!(body["imap"], json!({"host": "imap.example.test", "port": 993, "security": "ssl", "folder": "INBOX"}));
        assert_eq!(body["smtp"]["port"], Value::Null);
        assert_eq!(body["address"], json!("me@example.test"));
        assert!(connect_body("a@b.test", "", "", "", ("h", "", "ssl", ""), ("", "", "")).is_err());
        assert!(connect_body("a@b.test", "", "", "pw", ("h", "x", "ssl", ""), ("", "", "")).is_err());
        assert_eq!(policy_body("denylist", " a@b.test \n\nexample.org"), json!({"mode": "denylist", "entries": ["a@b.test", "example.org"]}));
        assert_eq!(limits_body("5", "").unwrap(), json!({"per_hour": 5, "per_day": null}));
        assert!(limits_body("-1", "").is_err());
        let evs = vec![NotifyEvent { id: "job_failed".into(), label: "x".into(), email: true }];
        assert_eq!(notifications_body(&evs), json!({"email": {"job_failed": true}}));
    }

    #[test]
    fn mailbox_cell_reads_the_admin_row() {
        assert_eq!(mailbox_state(&json!({"email_account": {"state": "connected", "admin_enabled": true}})), ("connected".to_string(), true));
        assert_eq!(mailbox_state(&json!({"email_account": {"state": "turned off by an administrator", "admin_enabled": false}})).1, false);
        assert_eq!(mailbox_state(&json!({})), ("—".to_string(), true));
    }
}
