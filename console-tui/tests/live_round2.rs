//! Round-2 contract against a LIVE hermetic gateway (DESIGN-v2 §6), read
//! through the console's own client and parsers — ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18541 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_round2 -- --ignored --nocapture --test-threads 1

use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::accounts::{accounts_from_payload, activity_from_payload};
use abstractgateway_console::store::email::Discovery;
use abstractgateway_console::store::{agent_defaults_from, workflows_from_payload};
use serde_json::json;

fn live_client() -> GatewayClient {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "refusing {url}: live tests run against a hermetic gateway, never the operator's"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN");
    let client = GatewayClient::new(&url, Some(&token));
    client.ping().expect("the hermetic gateway answers");
    client
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn accounts_active_and_activity_round_trip() {
    let c = live_client();
    // A user to switch (idempotent: an existing one is fine).
    let _ = c.create_user(
        &json!({"user_id": "r2alice", "roles": ["user"], "enabled": true,
                                  "email": "r2alice@example.test"}),
    );
    let rows = accounts_from_payload(&c.accounts().expect("GET /admin/accounts"))
        .expect("rows parse as §6");
    eprintln!(
        "accounts: {:?}",
        rows.iter()
            .map(|r| (r.id.clone(), r.kind.clone(), r.active_cell()))
            .collect::<Vec<_>>()
    );
    let alice = rows
        .iter()
        .find(|r| r.id == "r2alice")
        .expect("r2alice listed");
    assert_eq!(alice.kind_label(), "User");
    assert_eq!(alice.email_address.as_deref(), Some("r2alice@example.test"));
    // Own row: the switch is unavailable with the gateway's reason.
    let own = rows
        .iter()
        .find(|r| r.role == "admin")
        .expect("an admin row");
    assert!(own.refusal("suspend").is_some(), "{own:?}");
    // Deactivate, read back, reactivate.
    c.set_account_active("r2alice", "default", false, true)
        .expect("PUT active=false");
    let rows = accounts_from_payload(&c.accounts().unwrap()).unwrap();
    assert!(!rows.iter().find(|r| r.id == "r2alice").unwrap().active);
    c.set_account_active("r2alice", "default", true, true)
        .expect("PUT active=true");
    let rows = accounts_from_payload(&c.accounts().unwrap()).unwrap();
    assert!(rows.iter().find(|r| r.id == "r2alice").unwrap().active);
    // Your own account: 409 with a sentence.
    let e = c
        .set_account_active(&own.id, &own.tenant_id, false, true)
        .expect_err("own account refused");
    let msg = e
        .body
        .as_ref()
        .and_then(|b| b.get("message"))
        .and_then(|m| m.as_str())
        .unwrap_or_default()
        .to_string();
    eprintln!("own-account refusal: {msg}");
    assert!(!msg.is_empty(), "{e:?}");
    // Activity: r2alice's (the switches are account events) and mine.
    let a = activity_from_payload(
        &c.account_activity(Some(("r2alice", "default")), false, "", 100)
            .expect("GET activity"),
    )
    .expect("activity parses");
    eprintln!(
        "r2alice activity: {:?} note={:?}",
        a.events.iter().map(|e| e.title.clone()).collect::<Vec<_>>(),
        a.note
    );
    let me = activity_from_payload(
        &c.account_activity(None, false, "", 100)
            .expect("GET /me/activity"),
    )
    .expect("parses");
    eprintln!("my activity: {} events", me.events.len());
    let filtered =
        activity_from_payload(&c.account_activity(None, false, "sign_in", 100).unwrap()).unwrap();
    assert!(filtered.events.iter().all(|e| e.kind == "sign_in"));
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn workflows_carry_source_description_and_plain_default_rows() {
    let c = live_client();
    let w = workflows_from_payload(&c.bundles(false).expect("GET /bundles"));
    assert!(!w.rows.is_empty());
    for r in &w.rows {
        eprintln!(
            "{} | {} | {} | {}",
            r.bundle_id,
            r.name,
            r.source_text(),
            r.description
        );
        assert!(r.source.is_some(), "{} has no source", r.bundle_id);
    }
    let cfg = c.runtime_config().expect("GET /admin/runtime-config");
    let defaults = agent_defaults_from(&cfg);
    assert!(!defaults.is_empty());
    for d in &defaults {
        eprintln!("{:?} {:?} {:?}", d.label, d.state, d.state_text());
        assert!(d.label.is_some() && d.state.is_some(), "{d:?}");
    }
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn discovery_defaults_and_test_notification_sentence() {
    let c = live_client();
    let d = Discovery::from_value(
        &c.discover_my_email("someone@example.test")
            .expect("discover"),
    );
    let defaults = d.defaults.expect("§6 defaults");
    eprintln!("defaults: {defaults:?}");
    assert_eq!(defaults.imap.host, "imap.example.test");
    let t = c
        .test_my_notifications()
        .expect("POST /me/notifications/test");
    eprintln!("test notification: {t}");
    assert!(t.get("message").and_then(|m| m.as_str()).is_some(), "{t}");
}
