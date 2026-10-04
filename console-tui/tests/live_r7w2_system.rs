//! R7.2 system pages driven through the REAL worker against a LIVE
//! hermetic scratch gateway (untracked/round4/r7/w2/run_scratch_gateway.sh:
//! scratch HOME, fake `tailscale`, fake LM Studio upstream serving
//! `lmstudio/fake-tool-model`, no provider keys, no model loads). After
//! each action the gateway STATE is read back by direct HTTP. Ignored by
//! default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18811 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   R7W2_SHOTS_DIR=<dir> cargo test --test live_r7w2_system -- --ignored --test-threads 1

mod r7w2_harness;

use abstracttui::prelude::*;
use serde_json::{json, Value};

use abstractgateway_console::ui;
use r7w2_harness::{http, live};

fn net(url: &str, token: &str) -> Value {
    let (code, v) = http(url, token, "GET", "/network", None);
    assert_eq!(code, 200, "{v}");
    v
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn network_trust_proxies_and_origins_live() {
    let (mut h, url, token) = live(Size::new(120, 60));
    // A clean start: no manual origin, trust off.
    http(&url, &token, "POST", "/network", Some(json!({"allowed_origins": [], "trust_proxy": false})));
    h.ui.screen.set(ui::SCREEN_NETWORK);
    h.until("the network page", |_, s| s.contains("Reached through another address?"));
    // The read at connect predates the clean start: Check again (r).
    h.key(b"r");
    let s = h.until("the clean read", |_, s| s.contains("Advanced  no manual origin · local proxy only"));
    assert!(s.contains("Tailscale") && s.contains("scratch-mac.tail1234.ts.net"), "{s}");
    h.shoot("live-network");
    // Advanced is folded with nothing set: open it.
    h.key(b"a");
    h.until("advanced open", |_, s| s.contains("Allowed origins"));
    // Tab to the switch: modes → addresses → Check again → OpenAI API → input → Add origin → switch.
    for _ in 0..6 {
        h.key(b"\t");
    }
    h.key(b" ");
    h.until("trust on at the gateway", |_, _| {
        net(&url, &token)["reverse_proxy"]["trust_proxy"]["value"] == json!(true)
    });
    let s = h.until("the saved line", |_, s| s.contains("Saved · applies now"));
    assert!(s.contains("[x] Trust proxies on other machines"), "{s}");
    h.shoot("live-network-trust-on");
    h.key(b" ");
    h.until("trust off at the gateway", |_, _| {
        net(&url, &token)["reverse_proxy"]["trust_proxy"]["value"] == json!(false)
    });
    // The origin input (one Tab back from the switch is "Add origin", two the input).
    h.key(b"\x1b[Z");
    h.key(b"\x1b[Z");
    for ch in "ftp://bad.example".bytes() {
        h.key(&[ch]);
    }
    h.key(b"\r");
    // The gateway's 400 sentence, verbatim, under the input.
    let (code, refusal) = http(
        &url,
        &token,
        "POST",
        "/network",
        Some(json!({"allowed_origins": ["ftp://bad.example"]})),
    );
    assert_eq!(code, 400, "{refusal}");
    let sentence = refusal["refused_reason"].as_str().expect("refused_reason").to_string();
    let s = h.until("the refusal sentence", |_, s| {
        let flat: String = s.replace('│', " ").split_whitespace().collect();
        flat.contains(&sentence.split_whitespace().collect::<String>())
    });
    h.shoot("live-network-origin-refused");
    assert!(
        net(&url, &token)["reverse_proxy"]["allowed_origins"]["value"] == json!([]),
        "nothing saved:\n{s}"
    );
    // A good origin: End, erase the refused draft, type the new one.
    h.key(b"\x1b[F");
    for _ in 0.."ftp://bad.example".len() {
        h.key(b"\x7f");
    }
    for ch in "https://gw.example.com".bytes() {
        h.key(&[ch]);
    }
    h.key(b"\r");
    h.until("origin saved at the gateway", |_, _| {
        net(&url, &token)["reverse_proxy"]["allowed_origins"]["value"] == json!(["https://gw.example.com"])
    });
    let s = h.until("the origin listed", |_, s| s.contains("https://gw.example.com  ×"));
    h.shoot("live-network-origin-added");
    // Remove it: Tab back to the origin list, x.
    h.key(b"\x1b[Z");
    h.key(b"x");
    h.until("origin removed at the gateway", |_, _| {
        net(&url, &token)["reverse_proxy"]["allowed_origins"]["value"] == json!([])
    });
    let _ = s;
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn network_internet_asks_the_gateways_acknowledgement_live() {
    let (mut h, url, token) = live(Size::new(120, 60));
    h.ui.screen.set(ui::SCREEN_NETWORK);
    h.until("the network page", |_, s| s.contains("Who can reach this gateway"));
    let before = net(&url, &token)["configured"]["mode"].clone();
    // The mode list holds the keyboard: down to Internet, Enter.
    for _ in 0..3 {
        h.key(b"\x1b[B");
    }
    h.key(b"\r");
    let s = h.until("the Internet confirm", |_, s| {
        s.contains("Before you open the gateway to the internet")
    });
    assert!(s.contains("[y] I understand, use Internet mode"), "{s}");
    h.shoot("live-network-internet-confirm");
    assert_eq!(net(&url, &token)["configured"]["mode"], before, "nothing saved before y");
    h.key(b"n");
    let s = h.until("confirm closed", |_, s| !s.contains("Before you open the gateway"));
    assert_eq!(net(&url, &token)["configured"]["mode"], before, "n keeps:\n{s}");
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn multimodal_apply_recommended_and_clear_route_live() {
    let (mut h, url, token) = live(Size::new(120, 50));
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.until("the routes", |_, s| s.contains("input.text"));
    h.shoot("live-multimodal");
    // Apply recommended (keep mine) → the gateway's routes carry the plan.
    h.key(b"a");
    h.until("the apply prompt", |_, s| s.contains("Apply the framework's recommended routes"));
    h.key(b"\r");
    h.until("applied", |h, _| {
        h.store
            .journal
            .with_untracked(|j| j.iter().any(|e| e.action.contains("recommended")))
    });
    let (code, v) = http(&url, &token, "GET", "/config/capability-defaults", None);
    assert_eq!(code, 200);
    let text = v["routes"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["key"] == "input.text")
        .cloned()
        .unwrap();
    assert!(text["provider"].is_string() && text["model"].is_string(), "{text}");
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn sandbox_text_generation_against_the_fake_upstream_live() {
    let (mut h, _url, _token) = live(Size::new(120, 40));
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("fake-tool-model".into());
    h.ui.screen.set(ui::SCREEN_REVIEW);
    let s = h.until("the sandbox with the pickers on the pair", |_, s| {
        s.contains("Text Chat will use lmstudio / fake-tool-model.")
            && s.contains("▐lmstudio")
            && s.contains("▐fake-tool-model")
    });
    assert!(s.contains("Sandbox"), "{s}");
    // Tab into the page (the tab bar first), then g runs the test.
    h.key(b"\t");
    h.key(b"\t");
    h.key(b"g");
    let s = h.until("the reply", |_, s| s.contains("Hello there."));
    h.shoot("live-sandbox-text");
    let _ = s;
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn resources_reads_host_state_and_the_caches_tab_live() {
    let (mut h, url, token) = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_MODELS);
    let s = h.until("host state", |_, s| s.contains("RAM") && s.contains("Session caches"));
    assert!(s.contains("Models ("), "{s}");
    h.shoot("live-resources");
    let (code, v) = http(&url, &token, "GET", "/host/state", None);
    assert_eq!(code, 200, "{v}");
    let caches = v["session_caches"].as_array().map(Vec::len).unwrap_or(0);
    // The caches tab: the web's sentence when the host has none.
    h.ui.models_tab.set(1);
    let s = h.turns(3);
    if caches == 0 {
        assert!(s.contains("No session prompt caches right now."), "{s}");
        h.key(b"c");
        assert_eq!(
            h.store.notice.get_untracked().as_deref(),
            Some("no cache selected — nothing to clear")
        );
    }
    h.shoot("live-resources-caches");
}
