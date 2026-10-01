//! The Accounts screen's rows (DESIGN-v2 §2, contract §6): users and
//! entities in ONE list from `GET /admin/accounts`, and one account's
//! activity from `GET /admin/accounts/{id}/activity` or `GET /me/activity`.
//!
//! Parsing is contract-strict: a reply without the fields §6 names is a
//! protocol error with the missing field in its text (the screen shows it),
//! never a row rebuilt from guesses.

use serde_json::Value;

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}

/// One row action's availability (`actions.<name>`).
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Action {
    pub available: bool,
    /// Why it is unavailable (a sentence from the gateway).
    pub reason: Option<String>,
}

/// The row actions §6 lists, in the order the screen shows them. Accounts
/// are archived, never deleted (round 3): `archive` / `unarchive`.
pub const ACTIONS: [&str; 8] = [
    "email",
    "logs",
    "workspace",
    "rotate",
    "manage",
    "archive",
    "unarchive",
    "suspend",
];

#[derive(Clone, Debug, PartialEq, Default)]
pub struct Mailbox {
    /// "connected" | "not_connected" | "paused" | "unavailable".
    pub state: String,
    pub address: Option<String>,
    pub provider: Option<String>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct AccountRow {
    pub id: String,
    pub tenant_id: String,
    /// "user" | "entity".
    pub kind: String,
    /// "admin" | "user" | "entity".
    pub role: String,
    pub email_address: Option<String>,
    pub mailbox: Mailbox,
    pub runtime_id: Option<String>,
    pub active: bool,
    pub entity_state: Option<String>,
    /// Archived: can't sign in or act; records kept (round 3).
    pub archived: bool,
    /// (name, availability) for every name in [`ACTIONS`].
    pub actions: Vec<(String, Action)>,
}

impl AccountRow {
    pub fn from_value(v: &Value) -> Result<AccountRow, String> {
        let id = s(v, "id").ok_or("an account row has no id")?;
        let need = |k: &str| format!("account {id}: the gateway sent no `{k}`");
        let kind = s(v, "kind").ok_or_else(|| need("kind"))?;
        let role = s(v, "role").ok_or_else(|| need("role"))?;
        let active = v
            .get("active")
            .and_then(Value::as_bool)
            .ok_or_else(|| need("active"))?;
        let archived = v
            .get("archived")
            .and_then(Value::as_bool)
            .ok_or_else(|| need("archived"))?;
        let mb = v.get("mailbox").ok_or_else(|| need("mailbox"))?;
        let mailbox = Mailbox {
            state: s(mb, "state").ok_or_else(|| need("mailbox.state"))?,
            address: s(mb, "address"),
            provider: s(mb, "provider"),
            reason: s(mb, "reason"),
        };
        let acts = v.get("actions").ok_or_else(|| need("actions"))?;
        let mut actions = Vec::new();
        for name in ACTIONS {
            let a = acts
                .get(name)
                .ok_or_else(|| need(&format!("actions.{name}")))?;
            actions.push((
                name.to_string(),
                Action {
                    available: a
                        .get("available")
                        .and_then(Value::as_bool)
                        .ok_or_else(|| need(&format!("actions.{name}.available")))?,
                    reason: s(a, "reason"),
                },
            ));
        }
        Ok(AccountRow {
            tenant_id: s(v, "tenant_id").unwrap_or_else(|| "default".into()),
            email_address: s(v, "email_address").filter(|a| !a.trim().is_empty()),
            runtime_id: s(v, "runtime_id"),
            entity_state: s(v, "entity_state"),
            id,
            kind,
            role,
            active,
            archived,
            mailbox,
            actions,
        })
    }

    pub fn is_entity(&self) -> bool {
        self.kind == "entity"
    }

    /// The row's action `name` (every name in [`ACTIONS`] is present:
    /// the parser refuses a row without one).
    pub fn action(&self, name: &str) -> Action {
        self.actions
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, a)| a.clone())
            .unwrap_or_else(|| panic!("account action {name:?} is not in ACTIONS"))
    }

    /// Why action `name` cannot apply (None = available). An unavailable
    /// action without a reason from the gateway still says so.
    pub fn refusal(&self, name: &str) -> Option<String> {
        let a = self.action(name);
        if a.available {
            None
        } else {
            Some(
                a.reason
                    .unwrap_or_else(|| format!("{name} is not available for {}", self.id)),
            )
        }
    }

    /// The kind chip: "Admin" / "User" / "Entity".
    pub fn kind_label(&self) -> &'static str {
        match (self.kind.as_str(), self.role.as_str()) {
            ("entity", _) => "Entity",
            (_, "admin") => "Admin",
            _ => "User",
        }
    }

    /// The Mailbox cell in words.
    pub fn mailbox_cell(&self) -> String {
        match self.mailbox.state.as_str() {
            "connected" => match &self.mailbox.address {
                Some(a) => format!("Connected as {a}"),
                None => "Connected".into(),
            },
            "not_connected" => "Not connected".into(),
            "paused" => "Paused".into(),
            // Entities (and users whose mailbox cannot apply): the
            // reason lives in the Email view.
            _ => "—".into(),
        }
    }

    /// The Active cell: `Archived` / `[x]` / `[ ]` / `[-] <reason>`.
    pub fn active_cell(&self) -> String {
        if self.archived {
            return "Archived".into();
        }
        match self.refusal("suspend") {
            Some(why) => format!("[-] {why}"),
            None if self.active => "[x]".into(),
            None => "[ ]".into(),
        }
    }
}

/// `GET /admin/accounts` → rows in the gateway's order (admins, users,
/// entities, then id — the gateway sorts).
pub fn accounts_from_payload(v: &Value) -> Result<Vec<AccountRow>, String> {
    let rows = v
        .get("accounts")
        .and_then(Value::as_array)
        .ok_or("the gateway's /admin/accounts reply has no `accounts` list")?;
    rows.iter().map(AccountRow::from_value).collect()
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct ActivityEvent {
    pub ts: String,
    /// "sign_in" | "token" | "run" | "automation" | "email" | "account".
    pub kind: String,
    pub title: String,
    pub detail: Option<String>,
    pub run_id: Option<String>,
    pub observer_path: Option<String>,
    pub ok: bool,
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct ActivityData {
    pub events: Vec<ActivityEvent>,
    pub oldest_ts: Option<String>,
    pub truncated: bool,
    pub note: Option<String>,
}

/// The Logs filter chips: (label, `kind=` value; "" = all).
pub const ACTIVITY_FILTERS: [(&str, &str); 5] = [
    ("All", ""),
    ("Sign-ins", "sign_in"),
    ("Runs", "run"),
    ("Automations", "automation"),
    ("Email", "email"),
];

/// The empty state, in the web's words.
pub const ACTIVITY_EMPTY: &str = "No recorded activity yet. The gateway records sign-ins, changes, runs started and email events.";

/// The honest scope of the log (read-only requests are not recorded).
pub const ACTIVITY_SCOPE: &str =
    "Read-only requests (page views, token use on reads) are not recorded.";

pub fn activity_from_payload(v: &Value) -> Result<ActivityData, String> {
    let events = v
        .get("events")
        .and_then(Value::as_array)
        .ok_or("the gateway's activity reply has no `events` list")?;
    let mut out = Vec::new();
    for e in events {
        let title = s(e, "title").ok_or("an activity event has no `title`")?;
        out.push(ActivityEvent {
            ts: s(e, "ts").unwrap_or_default(),
            kind: s(e, "kind").unwrap_or_default(),
            detail: s(e, "detail"),
            run_id: s(e, "run_id"),
            observer_path: s(e, "observer_path"),
            ok: e.get("ok").and_then(Value::as_bool).unwrap_or(true),
            title,
        });
    }
    Ok(ActivityData {
        events: out,
        oldest_ts: s(v, "oldest_ts"),
        truncated: v.get("truncated").and_then(Value::as_bool).unwrap_or(false),
        note: s(v, "note"),
    })
}

/// "14:05" for today, "Sep 30 14:05" for an older day — the web's form,
/// in the VIEWER's local time. `ts` is ISO-8601 as the gateway writes it
/// (`2026-09-30T14:05:12+00:00`); `today` is the local `YYYY-MM-DD`
/// (`localtime::local_today`).
pub fn activity_time(ts: &str, today: &str) -> String {
    match crate::localtime::parse_iso_epoch(ts) {
        Some(e) => activity_time_at(e, crate::localtime::local_offset(e), today),
        None => ts.to_string(),
    }
}

/// `activity_time` for an instant and a fixed UTC offset (seconds).
pub fn activity_time_at(epoch: i64, offset: i64, today: &str) -> String {
    let (date, hm) = crate::localtime::parts_at(epoch, offset);
    let date = date.as_str();
    if date == today {
        return hm;
    }
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let mut parts = date.split('-');
    let (_, m, d) = (parts.next(), parts.next(), parts.next());
    match (
        m.and_then(|m| m.parse::<usize>().ok()),
        d.and_then(|d| d.parse::<u32>().ok()),
    ) {
        (Some(m @ 1..=12), Some(d)) => format!("{} {d} {hm}", MONTHS[m - 1]),
        _ => format!("{date} {hm}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    pub fn row(id: &str, kind: &str, role: &str) -> Value {
        let ok = json!({"available": true, "reason": null});
        json!({"id": id, "tenant_id": "default", "kind": kind, "role": role,
               "email_address": null,
               "mailbox": {"state": "not_connected", "address": null, "provider": null, "reason": null},
               "runtime_id": id, "active": true, "entity_state": null, "archived": false,
               "actions": {"email": ok, "logs": ok, "workspace": ok, "rotate": ok,
                           "manage": ok, "archive": ok, "unarchive": ok, "suspend": ok}})
    }

    #[test]
    fn parses_the_contract_row_and_its_cells() {
        let mut v = row("castor", "entity", "entity");
        v["actions"]["unarchive"] =
            json!({"available": false, "reason": "This account isn't archived."});
        v["mailbox"] = json!({"state": "unavailable", "address": null, "provider": null,
            "reason": "Mailboxes are turned off for this account (Email for everyone)."});
        let r = AccountRow::from_value(&v).unwrap();
        assert_eq!(r.kind_label(), "Entity");
        assert_eq!(r.mailbox_cell(), "—");
        assert_eq!(r.active_cell(), "[x]");
        assert_eq!(
            r.refusal("unarchive").as_deref(),
            Some("This account isn't archived.")
        );
        assert_eq!(r.refusal("archive"), None);
        assert_eq!(r.refusal("manage"), None);
    }

    #[test]
    fn an_archived_row_says_archived_and_has_no_delete() {
        let mut v = row("alice", "user", "user");
        v["archived"] = json!(true);
        v["active"] = json!(false);
        let r = AccountRow::from_value(&v).unwrap();
        assert!(r.archived);
        assert_eq!(r.active_cell(), "Archived");
        assert!(!ACTIONS.contains(&"delete"));
        // A row from a gateway that still sends `delete` and no `archive` is refused by name.
        let mut old = row("bob", "user", "user");
        old["actions"].as_object_mut().unwrap().remove("archive");
        let e = accounts_from_payload(&json!({"accounts": [old]})).unwrap_err();
        assert!(e.contains("actions.archive"), "{e}");
    }

    #[test]
    fn own_row_shows_the_unavailable_marker_with_its_reason() {
        let mut v = row("admin", "user", "admin");
        v["actions"]["suspend"] =
            json!({"available": false, "reason": "You can't deactivate your own account."});
        v["mailbox"] = json!({"state": "connected", "address": "a@x.test", "provider": "imap"});
        let r = AccountRow::from_value(&v).unwrap();
        assert_eq!(r.kind_label(), "Admin");
        assert_eq!(
            r.active_cell(),
            "[-] You can't deactivate your own account."
        );
        assert_eq!(r.mailbox_cell(), "Connected as a@x.test");
    }

    #[test]
    fn a_row_missing_a_contract_field_is_refused_by_name() {
        let mut v = row("alice", "user", "user");
        v["actions"].as_object_mut().unwrap().remove("suspend");
        let e = accounts_from_payload(&json!({"accounts": [v]})).unwrap_err();
        assert!(e.contains("actions.suspend"), "{e}");
        let e = accounts_from_payload(&json!({"users": []})).unwrap_err();
        assert!(e.contains("accounts"), "{e}");
    }

    #[test]
    fn activity_time_reads_like_the_web() {
        let at = |ts: &str| crate::localtime::parse_iso_epoch(ts).unwrap();
        assert_eq!(
            activity_time_at(at("2026-10-01T14:05:09+00:00"), 0, "2026-10-01"),
            "14:05"
        );
        assert_eq!(
            activity_time_at(at("2026-09-30T14:05:09+00:00"), 0, "2026-10-01"),
            "Sep 30 14:05"
        );
        // Local time, not UTC: 23:30 UTC is 01:30 the next day at UTC+2.
        assert_eq!(
            activity_time_at(at("2026-09-30T23:30:00Z"), 7200, "2026-10-01"),
            "01:30"
        );
        assert_eq!(
            activity_time_at(at("2026-09-30T23:30:00Z"), 0, "2026-10-01"),
            "Sep 30 23:30"
        );
    }

    #[test]
    fn activity_parses_events() {
        let d = activity_from_payload(&json!({"events": [
            {"ts": "2026-10-01T10:00:00Z", "kind": "run", "title": "Run started",
             "detail": "basic-agent", "run_id": "r1", "observer_path": "/runs/r1", "ok": true}
        ], "source": "audit_log", "oldest_ts": "2026-09-01T00:00:00Z", "truncated": false,
           "note": "From the gateway's audit log (last 30 days available)."}))
        .unwrap();
        assert_eq!(d.events[0].observer_path.as_deref(), Some("/runs/r1"));
        assert!(d.note.unwrap().contains("audit log"));
    }
}
