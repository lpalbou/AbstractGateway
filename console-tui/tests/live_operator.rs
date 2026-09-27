//! Live checks of the operator-control READ routes against a REAL gateway —
//! ignored by default (same convention as live_e2e.rs).
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18862 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_operator -- --ignored --nocapture --test-threads 1
//!
//! Nothing here writes: the host verbs (pause/resume, restart, quit), the
//! workflow import/reload, the backlog settings, the skills reseed and the
//! own-policy save are proven through the real binary under a pty (they
//! change gateway state and need the worker's watcher / verify path).

use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::operator::{tray_note, HostRunner, HostUpdate, MyPolicy};
use abstractgateway_console::store::NetworkData;

/// A HERMETIC gateway only: no default URL, and the operator's usual
/// ports (8080/8081) are refused, like the other live suites.
fn live_client() -> Option<GatewayClient> {
    let url = std::env::var("ABSTRACTGATEWAY_URL").ok()?;
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "refusing {url}: live tests run against a hermetic gateway, never the operator's"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").ok()?;
    let client = GatewayClient::new(&url, Some(&token));
    client.ping().ok()?;
    Some(client)
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn host_reads_parse_into_what_the_panel_shows() {
    let Some(c) = live_client() else {
        panic!("no live gateway (ABSTRACTGATEWAY_URL / ABSTRACTGATEWAY_AUTH_TOKEN)");
    };
    let r = HostRunner::from_value(&c.host_runner().expect("GET /host/runner"));
    println!(
        "runner: {} · restart={} shutdown={} {}",
        r.state_text(),
        r.cap_restart,
        r.cap_shutdown,
        r.cap_reason
    );
    let tray = tray_note(&c.host_tray().expect("GET /host/tray"));
    println!("tray: {tray}");
    assert!(!tray.is_empty());
    let u = HostUpdate::from_value(&c.host_update().expect("GET /host/update (admin)"));
    println!("version: {} · {}", u.version_text(), u.hint_text());
    assert!(!u.current.is_empty());
    let p = MyPolicy::from_value(&c.my_workspace_policy().expect("GET /workspace/policy/self"));
    println!(
        "my policy: {}:{} · {}",
        p.tenant_id,
        p.user_id,
        p.effective_text()
    );
    assert!(!p.user_id.is_empty());
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn public_lookup_reaches_the_gateway_and_says_why_when_it_does_not_run() {
    let Some(c) = live_client() else {
        panic!("no live gateway (ABSTRACTGATEWAY_URL / ABSTRACTGATEWAY_AUTH_TOKEN)");
    };
    let v = c
        .network_lookup_public()
        .expect("GET /network?lookup_public=1");
    let d = NetworkData::from_value(&v);
    let public = d.addresses.iter().find(|a| a.kind == "public");
    println!(
        "mode {} / {} · public row: {:?} · note: {:?} · discovery: {}",
        d.configured_mode,
        d.effective_mode,
        public.map(|a| (&a.url, &a.note)),
        d.public_note,
        v.get("discovery").cloned().unwrap_or_default()
    );
    // Either the gateway looked it up (internet mode: a `public` row), or
    // it says in words why it did not — never a silent nothing.
    assert!(public.is_some() || d.public_note.is_some());
}
