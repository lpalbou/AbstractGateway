//! A `/admin/accounts` reply (DESIGN-v2 §6) derived from seeded users and
//! entities fixtures, so tests that seed the users registry and the
//! entity roster also seed the Accounts table the screen now reads.
#![allow(dead_code)]

use abstractgateway_console::store::accounts::{accounts_from_payload, AccountRow};
use abstractgateway_console::store::{EntityRow, UsersData};
use serde_json::{json, Value};

pub const OWN_REASON: &str = "You can't deactivate your own account.";
pub const OWN_ARCHIVE_REASON: &str = "You can't archive your own account.";
pub const NOT_ARCHIVED_REASON: &str = "This account isn't archived.";
pub const ENTITY_ROTATE_REASON: &str =
    "An entity's token is never shown; it has no token to rotate here.";

fn ok() -> Value {
    json!({"available": true, "reason": null})
}
fn no(reason: &str) -> Value {
    json!({"available": false, "reason": reason})
}

pub fn user_row(
    id: &str,
    admin: bool,
    email: &str,
    mailbox: &str,
    active: bool,
    own: bool,
) -> Value {
    let (state, address) = match mailbox.strip_prefix("connected as ") {
        Some(a) => ("connected", Value::String(a.to_string())),
        None => ("not_connected", Value::Null),
    };
    json!({"id": id, "tenant_id": "default", "kind": "user",
           "role": if admin { "admin" } else { "user" },
           "email_address": if email.is_empty() { Value::Null } else { Value::String(email.into()) },
           "mailbox": {"state": state, "address": address, "provider": null, "reason": null},
           "runtime_id": id, "active": active, "entity_state": null, "archived": false,
           "actions": {"email": ok(), "logs": ok(), "workspace": ok(), "rotate": ok(),
                       "manage": no("Manage is for entities."),
                       "archive": if own { no(OWN_ARCHIVE_REASON) } else { ok() },
                       "unarchive": no(NOT_ARCHIVED_REASON),
                       "suspend": if own { no(OWN_REASON) } else { ok() }}})
}

pub fn entity_row(id: &str, state: &str, active: bool) -> Value {
    json!({"id": id, "tenant_id": "default", "kind": "entity", "role": "entity",
           "email_address": null,
           "mailbox": {"state": "not_connected", "address": null, "provider": null, "reason": null},
           "runtime_id": id, "active": active, "entity_state": state, "archived": false,
           "actions": {"email": ok(), "logs": ok(), "workspace": ok(),
                       "rotate": no(ENTITY_ROTATE_REASON), "manage": ok(),
                       "archive": ok(), "unarchive": no(NOT_ARCHIVED_REASON), "suspend": ok()}})
}

/// Admins, users, entities (the gateway's order); `own` is the signed-in
/// principal's id.
pub fn accounts_from(users: &UsersData, entities: &[EntityRow], own: &str) -> Vec<AccountRow> {
    let mut rows: Vec<Value> = Vec::new();
    let mut humans = users.humans.clone();
    humans.sort_by_key(|u| (!u.roles.iter().any(|r| r == "admin"), u.user_id.clone()));
    for u in &humans {
        rows.push(user_row(
            &u.user_id,
            u.roles.iter().any(|r| r == "admin"),
            &u.email,
            &u.mailbox,
            u.enabled,
            u.user_id == own,
        ));
    }
    for e in entities {
        let id = e.slug.clone().unwrap_or_else(|| e.name.clone());
        rows.push(entity_row(&id, &e.state, e.state != "paused"));
    }
    accounts_from_payload(&json!({ "accounts": rows })).expect("fixture rows are §6-shaped")
}

/// Keep `store.accounts` derived from the seeded users + entities (the
/// signed-in principal read from the connection).
pub fn mirror(cx: abstracttui::prelude::Scope, store: abstractgateway_console::store::Store) {
    use abstractgateway_console::store::{ConnPhase, Loadable};
    cx.effect(move || {
        let users = store.users.get();
        let entities = store.entities.get();
        let own = store.conn.with(|c| match c {
            ConnPhase::Connected(id) | ConnPhase::Verifying(id) => id.user_id.clone(),
            _ => String::new(),
        });
        let (Loadable::Ready(u), e) = (users, entities) else {
            return;
        };
        let e = e.ready().cloned().unwrap_or_default();
        store
            .accounts
            .set(Loadable::Ready(accounts_from(&u, &e, &own)));
    });
}
