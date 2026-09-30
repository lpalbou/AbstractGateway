//! Round-2 buffer snapshots: the screen list (§1) and the Accounts screen
//! (§2). `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_accounts -- --ignored`.

mod r2shots;

use abstractgateway_console::store::{entities_from_payload, users_from_payload, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::{harness, Harness, SIZES};
use serde_json::{json, Value};

fn users_payload() -> Value {
    json!({"users": [
        {"user_id": "admin", "tenant_id": "default", "email": "admin@example.test", "roles": ["admin", "user"],
         "enabled": true, "runtime_id": "default",
         "email_account": {"configured": true, "address": "admin@example.test", "state": "connected",
                           "admin_enabled": true, "agent_tools_available": true}},
        {"user_id": "alice", "tenant_id": "default", "email": "alice@example.test", "roles": ["user"],
         "enabled": true, "runtime_id": "alice",
         "email_account": {"configured": false, "address": "", "state": "not connected",
                           "admin_enabled": true, "agent_tools_available": false}},
        {"user_id": "castor", "tenant_id": "default", "roles": ["entity"], "principal_kind": "entity",
         "enabled": true, "runtime_id": "castor"}
    ]})
}

fn entities_payload() -> Value {
    json!({"entities": [
        {"name": "castor", "handle": "castor@gw", "state": {"state": "awake", "mode": "resting"},
         "drives": {"questions": {"open": 2}, "problems": {"open": 0}, "interests": {"open": 1}}}
    ]})
}

pub fn screen(size: (i32, i32), idx: usize) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(idx);
    h.turns(2);
    h
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_screen_list() {
    for size in SIZES {
        let mut h = screen(size, ui::SCREEN_CONNECTION);
        h.shoot("nav-connection");
        let mut h = screen(size, ui::SCREEN_NETWORK);
        h.shoot("nav-network");
    }
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_accounts_before() {
    for size in SIZES {
        let mut h = screen(size, ui::SCREEN_USERS);
        h.store
            .users
            .set(Loadable::Ready(users_from_payload(&users_payload())));
        h.store
            .entities
            .set(Loadable::Ready(entities_from_payload(&entities_payload())));
        h.turns(3);
        h.shoot("accounts");
    }
}
