//! Live Apps-screen proof against a HERMETIC gateway — ignored by default.
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18863 \
//!   ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_apps -- --ignored --nocapture --test-threads 1
//!
//! The same client calls and the same parse/rules the Apps screen's worker
//! runs (api_apps.rs, store_apps.rs). The lifecycle test INSTALLS an app
//! into the gateway's data dir: it refuses the usual ports (8080/8081)
//! and requires an explicit ABSTRACTGATEWAY_URL — point it only at a
//! throwaway gateway started with its own ABSTRACTGATEWAY_DATA_DIR.

use std::time::{Duration, Instant};

use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::apps::{
    app_error_note, primary_verb, secondary_verbs, AppJob, AppLog, AppOpenLink, AppVerb, AppsOverview,
};

fn client() -> GatewayClient {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("set ABSTRACTGATEWAY_URL (a hermetic gateway)");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "refusing {url}: the live apps tests install and start apps — hermetic gateways only"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("set ABSTRACTGATEWAY_AUTH_TOKEN");
    let c = GatewayClient::new(&url, Some(&token));
    c.ping().expect("gateway answers /ping");
    c
}

fn overview(c: &GatewayClient, latest: bool) -> AppsOverview {
    AppsOverview::from_value(&c.apps_overview(latest).expect("GET /apps"))
}

fn wait_job(c: &GatewayClient, id: &str, limit: Duration) -> AppJob {
    let t0 = Instant::now();
    loop {
        let j = AppJob::from_value(c.apps_job(id).expect("GET /apps/jobs/{id}").get("job").unwrap()).unwrap();
        println!("  job {id}: {}", j.progress_line("job"));
        if !j.is_active() {
            return j;
        }
        assert!(t0.elapsed() < limit, "job {id} still {} after {limit:?}", j.state);
        std::thread::sleep(Duration::from_secs(1));
    }
}

/// Every row, its state and the verbs the screen offers (admin).
#[test]
#[ignore]
fn live_apps_list_states_and_verbs() {
    let c = client();
    let o = overview(&c, true);
    println!("node: available={} version={:?} source={}", o.node.available, o.node.version, o.node.source);
    println!("registry reachable={:?} install_allowed={} apps_host={:?}", o.registry_reachable, o.install_allowed, o.apps_host);
    assert!(!o.apps.is_empty(), "the gateway lists apps");
    for a in &o.apps {
        let p = primary_verb(a, a.active_job.as_ref(), true);
        let sec = secondary_verbs(a, a.active_job.as_ref(), None, true);
        println!(
            "{:10} kind={:7} status={:13} source={:?} external_port={:?} primary={:?} secondary=[{}]",
            a.id,
            a.kind,
            a.status,
            a.source,
            a.external_port,
            p.map(|v| (v.label, v.available.is_ok())),
            sec.iter()
                .map(|v| format!("{}:{}", v.label, if v.available.is_ok() { "on" } else { "off" }))
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    // An app started outside the gateway: the screen offers no Stop, and
    // the route itself refuses — the screen's note keeps its words.
    if let Some(ext) = o.apps.iter().find(|a| a.is_external()) {
        let e = c.apps_stop(&ext.id).expect_err("stop of an external app is refused");
        let note = app_error_note(&format!("stop {}", ext.name), &e);
        println!("refusal note: {} | hint: {:?}", note.text, note.hint);
        assert_eq!(e.status(), Some(409));
        assert_eq!(e.body.as_ref().and_then(|b| b.get("reason")).and_then(|r| r.as_str()), Some("started_outside_gateway"));
    }
}

/// Install job start + cancel (the job ends `cancelled`, nothing changes).
#[test]
#[ignore]
fn live_apps_install_then_cancel() {
    let c = client();
    let app = "observer".to_string();
    let before = overview(&c, false);
    let row = before.apps.iter().find(|a| a.id == app).expect("app listed");
    assert!(!row.installed, "{app} must not be installed on this hermetic gateway");
    let v = c.apps_install(&app).expect("POST /apps/{id}/install");
    let job = AppJob::from_value(v.get("job").unwrap()).unwrap();
    println!("install job {} started (created={:?})", job.id, v.get("created"));
    let v = c.apps_job_cancel(&job.id).expect("POST cancel");
    println!("cancel answered: {}", AppJob::from_value(v.get("job").unwrap()).unwrap().state);
    let end = wait_job(&c, &job.id, Duration::from_secs(120));
    println!("job {} ended: {} parts={:?}", end.id, end.state, end.parts);
    assert_eq!(end.state, "cancelled");
    let after = overview(&c, false);
    assert!(!after.apps.iter().find(|a| a.id == app).unwrap().installed, "cancelled: not installed");
}

/// Install → start → open (one-time link) → log → stop, each verified by
/// a read-back, on the smallest app (Code: 0.6 MB, no npm dependencies).
#[test]
#[ignore]
fn live_apps_lifecycle() {
    let c = client();
    let app = "code".to_string();
    let o = overview(&c, true);
    let row = o.apps.iter().find(|a| a.id == app).expect("app listed").clone();
    assert!(!row.installed && !row.is_external(), "{app} must be absent on this hermetic gateway: {row:?}");
    assert_eq!(primary_verb(&row, None, true).unwrap().verb, AppVerb::Install);

    let v = c.apps_install(&app).expect("install");
    let job = AppJob::from_value(v.get("job").unwrap()).unwrap();
    let end = wait_job(&c, &job.id, Duration::from_secs(600));
    println!("install ended {} result_version={:?} terminal={} parts={:?}", end.state, end.result_version, end.result_terminal, end.parts);
    assert_eq!(end.state, "succeeded", "error={:?} log={}", end.error, end.log_text());
    let row = overview(&c, false).apps.into_iter().find(|a| a.id == app).unwrap();
    println!("after install: status={} version={:?} actions={:?}", row.status, row.version, row.actions);
    assert!(row.installed && !row.running);
    let p = primary_verb(&row, None, true).unwrap();
    assert_eq!((p.verb, p.available.is_ok()), (AppVerb::Open, true), "Open starts it first");

    let v = c.apps_launch(&app).expect("launch");
    println!("launch answered running={:?} url={:?}", v["app"]["running"], v["app"]["url"]);
    let row = overview(&c, false).apps.into_iter().find(|a| a.id == app).unwrap();
    assert!(row.running, "GET /apps says running: {row:?}");

    let v = c.apps_open(&app, None).expect("open");
    let link = AppOpenLink::from_value(c.base_url(), &app, &row.name, false, &v).expect("open_url");
    println!("one-time link: {} (app {:?}, {:?}s)\n  hint: {:?}", link.link, link.app_url, link.expires_in_s, link.tunnel_hint);
    assert!(link.link.contains("/apps/handover/"));

    let v = c.apps_logs(&app, 200).expect("logs");
    let log = AppLog::from_value(&app, 200, &v);
    println!("log: {} · file {:?}", log.head(), log.path);

    let v = c.apps_stop(&app).expect("stop");
    println!("stop answered running={:?}", v["app"]["running"]);
    let row = overview(&c, false).apps.into_iter().find(|a| a.id == app).unwrap();
    assert!(!row.running, "GET /apps says stopped: {row:?}");
    // Open of a stopped app without starting it: the route's own refusal.
    let e = c.apps_open(&app, None).expect_err("not running");
    println!("open refused: {}", app_error_note("open", &e).text);
}
