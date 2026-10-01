//! Entity RBAC (operator ruling 2026-10-01) buffer snapshots: a NON-admin's
//! Accounts screen is `/me/accounts` — themself + the entities they created.
//! `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_rbac -- --ignored`.

mod r2shots;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::{entities_from_payload, ConnPhase, Identity, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::{harness, SIZES};
use serde_json::{json, Value};

fn act(ok: bool, why: &str) -> Value {
    if ok {
        json!({"available": true, "reason": null})
    } else {
        json!({"available": false, "reason": why})
    }
}

/// `GET /me/accounts` as alice (the gateway's row shape).
fn my_accounts() -> Value {
    json!({"scope": "own", "accounts": [
        {"id": "alice", "tenant_id": "default", "kind": "user", "role": "user", "own": true,
         "email_address": "alice@example.test",
         "mailbox": {"state": "connected", "address": "alice@example.test", "provider": "imap", "reason": null},
         "runtime_id": "alice", "active": true, "entity_state": null,
         "actions": {"email": act(true, ""), "logs": act(true, ""),
                     "workspace": act(true, ""),
                     "rotate": act(false, "Only an admin can rotate your token."),
                     "manage": act(false, "Only entities have a management page."),
                     "delete": act(false, "You can't delete your own account."),
                     "suspend": act(false, "You can't deactivate your own account.")}},
        {"id": "aster", "tenant_id": "default", "kind": "entity", "role": "entity", "own": false,
         "email_address": null,
         "mailbox": {"state": "unavailable", "address": null, "provider": null,
                     "reason": "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime."},
         "runtime_id": "aster", "active": true, "entity_state": "asleep",
         "created_by": {"tenant_id": "default", "user_id": "alice"},
         "actions": {"email": act(false, "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime."),
                     "logs": act(true, ""), "workspace": act(true, ""),
                     "rotate": act(false, "An entity has no token to rotate: its credential is discarded when it is created and no one holds it."),
                     "manage": act(true, ""),
                     "delete": act(false, "An entity's name is kept for life; suspend it instead."),
                     "suspend": act(false, "Only an admin can suspend an entity.")}}
    ]})
}

fn entities_payload() -> Value {
    json!({"entities": [
        {"name": "Aster", "slug": "aster", "handle": "aster@gw", "state": {"state": "asleep"},
         "created_by": {"tenant_id": "default", "user_id": "alice"}}
    ]})
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_non_admin_accounts() {
    for size in SIZES {
        for (sel, name) in [
            (0usize, "accounts-non-admin-self"),
            (1, "accounts-non-admin-entity"),
        ] {
            let mut h = harness(Size::new(size.0, size.1));
            let id = Identity::from_me(&json!({
                "principal": {"user_id": "alice", "tenant_id": "default", "roles": ["user"], "admin": false},
                "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
            }))
            .unwrap();
            h.store.conn.set(ConnPhase::Connected(id));
            h.turns(1);
            h.ui.wizard.set(false);
            h.ui.screen.set(ui::SCREEN_USERS);
            h.turns(2);
            h.store
                .entities
                .set(Loadable::Ready(entities_from_payload(&entities_payload())));
            h.store.accounts.set(Loadable::Ready(
                accounts_from_payload(&my_accounts()).unwrap(),
            ));
            h.turns(2);
            h.ui.account_sel.set(sel);
            h.turns(3);
            let s = h.turns(1);
            assert!(s.contains("not an admin"), "{s}");
            assert!(s.contains("aster"), "{s}");
            h.shoot(name);
        }
    }
}
