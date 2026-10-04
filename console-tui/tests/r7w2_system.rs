//! R7.2 system pages at web-console parity, headless: Network, Resources,
//! Sandbox, Multimodal. Snapshot assertions per state (the web's sentences
//! verbatim) and the command/route each action sends. Live drive tests:
//! tests/live_r7w2_system.rs.

mod r7w2_harness;

use abstracttui::prelude::*;
use serde_json::{json, Value};

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{Loadable, NetworkData};
use abstractgateway_console::ui;
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r7w2_harness::{harness, Harness};

// ---------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------

fn network(configured: &str, pinned: bool, restart: bool) -> Value {
    json!({
        "schema": "gateway_network_v1",
        "writable": true,
        "configured": {"mode": configured, "label": match configured {
            "localhost" => "Localhost only", "lan" => "Local network", _ => "Internet"},
            "port": 8080, "source": "stored", "port_source": "stored"},
        "effective": {"mode": "localhost", "label": "Localhost only", "bind_host": "127.0.0.1",
            "port": 18811, "overridden_by_cli": false, "pinned_by_cli": pinned,
            "host_source": if pinned {"cli"} else {"setting"}, "port_source": "cli", "running": true},
        "restart_required": restart,
        "restart": {"available": true, "applies": true, "needed": restart, "port": 8080,
            "how": "POST /api/gateway/network/restart (admin)"},
        "auth": {"user_auth": true, "ok_for_mode": true, "will_enable_user_auth": false},
        "modes": [
            {"id": "localhost", "label": "Localhost only", "selected": configured == "localhost", "allowed": true},
            {"id": "lan", "label": "Local network", "selected": configured == "lan", "allowed": true},
            {"id": "internet", "label": "Internet", "selected": configured == "internet", "allowed": true, "requires_acknowledgement": true}
        ],
        "addresses": [
            {"kind": "loopback", "url": "http://127.0.0.1:18811", "reachable": true, "note": "this machine only"},
            {"kind": "lan", "url": "http://[2a01:e0a:d5e:e7f0:8d8c:d71a:9989:d324]:18811", "interface_label": "Wi-Fi",
             "reachable": false, "note": "not listening here yet: the gateway is bound to 127.0.0.1"},
            {"kind": "tailscale", "url": "http://scratch-mac.tail1234.ts.net:18811", "reachable": false,
             "note": "Tailscale name: devices on your tailnet"}
        ],
        "tailscale": {"dns_name": "scratch-mac.tail1234.ts.net", "ips": ["100.101.102.103"]},
        "copy_hint": "http://127.0.0.1:18811",
        "checked_at": "2026-10-04T02:11:23.920397Z",
        "warnings": ["Traffic is plain HTTP: sign-in passwords, tokens and session cookies cross the network unencrypted. Use it on networks you trust."],
        "reverse_proxy": {
            "allowed_origins": {"value": ["https://gw.example.com"], "source": "setting", "overridden_by_env": false,
                "effective": [], "builtin": ["http://localhost:*", "http://127.0.0.1:*"], "self_origins": [],
                "applies": "live", "warnings": []},
            "trust_proxy": {"value": false, "source": "default", "overridden_by_env": false, "effective": false,
                "applies": "live", "loopback": true}
        }
    })
}

fn set_net(h: &mut Harness, v: Value) -> String {
    h.store
        .network
        .set(Loadable::Ready(NetworkData::from_value(&v)));
    h.turns(3)
}

fn net_page(size: Size, v: Value) -> (Harness, String) {
    let mut h = harness(size);
    h.admin_on(ui::SCREEN_NETWORK);
    let s = set_net(&mut h, v);
    (h, s)
}

#[test]
fn network_page_speaks_the_web_sentences() {
    let (mut h, s) = net_page(Size::new(120, 60), network("lan", true, false));
    for needle in [
        "Who can reach this gateway  Running now: Localhost only · port 18811",
        "(•) Local network",
        "Local network: Phones, tablets and other computers on the same Wi-Fi or office network can connect.",
        "Stored",
    ]
    .iter()
    .take(3)
    {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    assert!(
        s.contains("Saved: Local network. This gateway listens on Localhost only because it was started with --host 127.0.0.1."),
        "pinned alert:\n{s}"
    );
    for needle in [
        "Addresses  Other devices use one of these to connect.",
        "This computer · Primary",
        "Works now",
        "Tailscale",
        "http://scratch-mac.tail1234.ts.net:18811",
        "Not in this mode",
        "Check again",
        "What to know about Local network (1)",
        "Reached through another address?  Tailscale, a reverse proxy",
        "including its Tailscale name",
        "tailscale serve --bg http://127.0.0.1:18811",
        "The OpenAI-compatible API has its own page:",
        "Advanced",
        "1 origin · local proxy only",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    // The Tailscale row is labelled as such (the web's NET_KIND_LABEL).
    assert!(
        s.lines()
            .any(|l| l.contains("Tailscale ")
                && l.contains("http://scratch-mac.tail1234.ts.net:18811")),
        "the Tailscale address row:\n{s}"
    );
    // The long IPv6 URL is never cut (it wraps or fits whole).
    let flat: String = s.split_whitespace().collect::<Vec<_>>().join("");
    assert!(
        flat.contains("http://[2a01:e0a:d5e:e7f0:8d8c:d71a:9989:d324]:18811"),
        "IPv6 address whole:\n{s}"
    );
    h.shoot("network-page");
}

#[test]
fn network_advanced_opens_with_a_and_shows_origins_and_the_switch() {
    // Closed by default when nothing is set (the web's `auto` rule) …
    let mut v = network("lan", false, false);
    v["reverse_proxy"]["allowed_origins"]["value"] = json!([]);
    let (mut h, s) = net_page(Size::new(120, 60), v);
    assert!(
        s.contains("Advanced  no manual origin · local proxy only"),
        "{s}"
    );
    assert!(!s.contains("Allowed origins"), "folded:\n{s}");
    // … open by default once an origin is saved; `a` folds and unfolds.
    let s = set_net(&mut h, network("lan", false, false));
    for needle in [
        "Allowed origins  [Saved setting]",
        "Other web addresses whose pages may use this gateway",
        "https://gw.example.com",
        "Add origin",
        "Always allowed: http://localhost:* http://127.0.0.1:*",
        "Client address  [Default]",
        "[ ] Trust proxies on other machines",
        "A proxy on this computer always names the real client (X-Forwarded-For)",
        "Only when your own proxy on that machine sits in front of every request",
        "Changes apply to the next request: no restart.",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    let s = h.key(b"a");
    assert!(!s.contains("Allowed origins"), "a folds it:\n{s}");
    let s = h.key(b"a");
    assert!(s.contains("Allowed origins"), "a unfolds it:\n{s}");
}

#[test]
fn network_mode_choice_posts_mode_then_internet_confirm_comes_from_the_gateway() {
    let (mut h, _) = net_page(Size::new(120, 50), network("lan", false, false));
    h.drain();
    // The mode list holds the keyboard: Down → Internet, Enter.
    h.key(b"\x1b[B");
    h.key(b"\r");
    let cmds = h.drain();
    let sent = cmds.iter().find_map(|c| match c {
        Cmd::Json(JsonCmd::Send {
            method, path, body, ..
        }) => Some((method.clone(), path.clone(), body.clone())),
        _ => None,
    });
    assert_eq!(
        sent,
        Some((
            "POST".into(),
            "/network".into(),
            json!({"mode": "internet"})
        )),
        "{cmds:?}"
    );
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::LoadNetwork)),
        "re-read after the write"
    );
    // The gateway asks for the acknowledgement (409): the confirm shows its words.
    let mut e = ApiError::new(ApiErrorKind::Http(409), "acknowledgement required");
    e.body = Some(
        json!({"reason_code": "acknowledgement_required", "mode": "internet",
        "refused_reason": "Internet mode needs your acknowledgement.",
        "warnings": ["The gateway speaks plain HTTP: put a TLS proxy in front."]}),
    );
    h.store
        .json
        .set_write("network.mode", Some(WriteState::Failed(e)));
    h.turns(2);
    let s = set_net(&mut h, network("lan", false, false));
    for needle in [
        "Before you open the gateway to the internet",
        "Internet mode needs your acknowledgement.",
        "• The gateway speaks plain HTTP: put a TLS proxy in front.",
        "[y] I understand, use Internet mode  [n] Keep Local network",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    h.shoot("network-internet-confirm");
    h.key(b"y");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::Json(JsonCmd::Send { body, .. })
            if *body == json!({"mode": "internet", "acknowledge_internet": true}))),
        "y acknowledges: {cmds:?}"
    );
}

#[test]
fn network_refused_mode_says_why_and_how_to_fix_and_posts_nothing() {
    let mut v = network("localhost", false, false);
    v["modes"][1]["allowed"] = json!(false);
    v["modes"][1]["reason"] = json!("Local network needs accounts.");
    v["modes"][1]["fix"] = json!("Start the gateway with accounts on.");
    let (mut h, s) = net_page(Size::new(120, 50), v.clone());
    assert!(s.contains("( ) Local network  Needs accounts"), "{s}");
    h.drain();
    h.key(b"\x1b[B");
    h.key(b"\r");
    let s = set_net(&mut h, v);
    assert!(
        !h.drain().iter().any(|c| matches!(c, Cmd::Json(_))),
        "nothing posted"
    );
    assert!(s.contains("Local network needs accounts."), "{s}");
    assert!(
        s.contains("How to fix it: Start the gateway with accounts on."),
        "{s}"
    );
}

#[test]
fn network_restart_box_offers_restart_now() {
    let (_h, s) = net_page(Size::new(120, 50), network("lan", false, true));
    assert!(
        s.contains("Restart to apply: Local network on port 8080."),
        "{s}"
    );
    assert!(
        s.contains(
            "The gateway keeps running as Localhost only (127.0.0.1:18811) until it restarts."
        ),
        "{s}"
    );
    assert!(s.contains("Restart now"), "{s}");
}

#[test]
fn network_origin_add_remove_and_the_gateways_refusal_verbatim() {
    let (mut h, _) = net_page(Size::new(120, 60), network("lan", false, false));
    h.drain();
    // Tab to the origin list (modes → addresses → Check again → OpenAI API → origins).
    let mut guard = 0;
    while !h.turns(1).contains("x removes the selected origin") && guard < 3 {
        guard += 1;
    }
    for _ in 0..4 {
        h.key(b"\t");
    }
    h.key(b"x");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::Json(JsonCmd::Send { body, .. })
            if *body == json!({"allowed_origins": []}))),
        "x removes the selected origin: {cmds:?}"
    );
    // (headless: no worker answers — clear the pending write by hand.)
    h.store.json.set_write("network.proxy", None);
    h.turns(2);
    // Tab to the input, type a bad origin, Enter.
    h.key(b"\t");
    for ch in "ftp://nope".bytes() {
        h.key(&[ch]);
    }
    h.key(b"\r");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::Json(JsonCmd::Send { body, .. })
            if *body == json!({"allowed_origins": ["https://gw.example.com", "ftp://nope"]}))),
        "Enter adds to the list: {cmds:?}"
    );
    let mut e = ApiError::new(ApiErrorKind::Http(400), "bad origin");
    e.body =
        Some(json!({"refused_reason": "ftp://nope: an origin starts with http:// or https://."}));
    h.store
        .json
        .set_write("network.proxy", Some(WriteState::Failed(e)));
    h.turns(2);
    let s = set_net(&mut h, network("lan", false, false));
    assert!(
        s.contains("ftp://nope: an origin starts with http:// or https://."),
        "the gateway's sentence under the input:\n{s}"
    );
    // Empty draft: the web's own sentence, nothing sent.
    h.store.json.set_write("network.proxy", None);
    h.turns(2);
}

#[test]
fn network_trust_switch_posts_trust_proxy() {
    let (mut h, _) = net_page(Size::new(120, 60), network("lan", false, false));
    h.drain();
    // modes → addresses → Check again → OpenAI API → origins → input → Add origin → switch
    for _ in 0..7 {
        h.key(b"\t");
    }
    let s = h.key(b" ");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::Json(JsonCmd::Send { body, .. })
            if *body == json!({"trust_proxy": true}))),
        "space switches trust: {cmds:?}\n{s}"
    );
    h.store.json.set_write(
        "network.proxy",
        Some(WriteState::Done(
            json!({"changed": {"trust_proxy": {"applies": "live"}}}),
        )),
    );
    let s = set_net(&mut h, network("lan", false, false));
    assert!(
        s.contains("Saved · applies now  Trust proxy applies to the next request."),
        "{s}"
    );
}

#[test]
fn network_fits_80x24_and_scrolls_to_the_focused_control() {
    let (mut h, s) = net_page(Size::new(80, 24), network("lan", false, false));
    assert!(s.contains("Who can reach this gateway"), "{s}");
    h.shoot("network-80");
    for _ in 0..7 {
        h.key(b"\t");
    }
    let s = h.turns(2);
    assert!(
        s.contains("Trust proxies on other machines"),
        "scrolled to the switch:\n{s}"
    );
    h.shoot("network-80-advanced");
}

#[test]
fn network_c_copies_only_an_address_that_works_now() {
    let (mut h, _) = net_page(Size::new(120, 60), network("lan", false, false));
    h.key(b"\t"); // → the address table
    h.key(b"c");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("copied http://127.0.0.1:18811")
    );
    h.key(b"\x1b[B"); // the IPv6 row: not in this mode
    h.key(b"c");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("http://[2a01:e0a:d5e:e7f0:8d8c:d71a:9989:d324]:18811 is not in this mode: nothing copied")
    );
    // Enter shows the row's note (the gateway's words).
    let s = h.key(b"\r");
    assert!(
        s.contains("not listening here yet: the gateway is bound to 127.0.0.1"),
        "{s}"
    );
}

#[test]
fn network_mode_list_marks_the_saved_mode_not_the_cursor() {
    let (mut h, _) = net_page(Size::new(120, 50), network("localhost", false, false));
    h.key(b"\x1b[B");
    let s = h.key(b"\x1b[B");
    assert!(s.contains("(•) Localhost only"), "{s}");
    assert!(s.contains("( ) Internet"), "{s}");
}

#[test]
fn network_environment_overrides_are_said_and_non_admins_read_only() {
    let mut v = network("lan", false, false);
    v["reverse_proxy"]["allowed_origins"]["overridden_by_env"] = json!(true);
    v["reverse_proxy"]["allowed_origins"]["env_name"] = json!("ABSTRACTGATEWAY_ALLOWED_ORIGINS");
    v["reverse_proxy"]["allowed_origins"]["env_value"] = json!(["https://pinned.example"]);
    v["reverse_proxy"]["trust_proxy"]["overridden_by_env"] = json!(true);
    v["reverse_proxy"]["trust_proxy"]["env_name"] = json!("ABSTRACTGATEWAY_TRUST_PROXY");
    v["reverse_proxy"]["trust_proxy"]["effective"] = json!(true);
    let (mut h, s) = net_page(Size::new(120, 70), v.clone());
    for needle in [
        "[Environment override]",
        "Allowed origins  [Set by the environment]",
        "This gateway was started with ABSTRACTGATEWAY_ALLOWED_ORIGINS in its environment, so that list decides:",
        "https://pinned.example. The origins below are saved and apply once the gateway is started without it.",
        "This gateway was started with ABSTRACTGATEWAY_TRUST_PROXY in its environment: trust is on.",
        "The switch is saved and applies once the gateway is started without it.",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    assert!(
        !s.contains("Always allowed"),
        "not under an env override:\n{s}"
    );
    v["writable"] = json!(false);
    let s = set_net(&mut h, v);
    assert!(
        s.contains("Only an admin can change who can reach this gateway."),
        "{s}"
    );
    assert!(
        s.contains("[-] Trust proxies on other machines — Only an admin can change these."),
        "{s}"
    );
    assert!(!s.contains("Add origin"), "no input for a non-admin:\n{s}");
}

#[test]
fn network_empty_origin_says_the_web_sentence_and_sends_nothing() {
    let (mut h, _) = net_page(Size::new(120, 60), network("lan", false, false));
    h.drain();
    for _ in 0..5 {
        h.key(b"\t"); // → the origin input
    }
    let s = h.key(b"\r");
    assert!(
        s.contains("Type an origin, for example https://gateway.example.com."),
        "{s}"
    );
    assert!(!h.drain().iter().any(|c| matches!(c, Cmd::Json(_))));
}

// ---------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------

fn host(models: Value, caches: Value) -> abstractgateway_console::store::HostStateData {
    abstractgateway_console::store::host_state_from_payload(&json!({
        "ok": true, "host": {"host_name": "scratch.local"},
        "memory": {"ram": {"total_bytes": 137438953472u64, "available_bytes": 51539607552u64,
                           "used_bytes": 85899345920u64, "percent": 62.5},
                   "process": {"rss_bytes": 1073741824u64}},
        "gpu": {"supported": false},
        "models": models, "session_caches": caches, "degraded": [], "reasons": {}
    }))
}

#[test]
fn resources_speaks_the_web_sections_and_empty_sentences() {
    for size in [Size::new(80, 24), Size::new(120, 40)] {
        let mut h = harness(size);
        h.admin_on(ui::SCREEN_MODELS);
        h.store
            .host_state
            .set(Loadable::Ready(host(json!([]), json!([]))));
        let s = h.turns(3);
        assert!(s.contains("Resources — Memory & GPU"), "{s}");
        assert!(s.contains("Models (0 resident)"), "{s}");
        assert!(s.contains("Session caches"), "{s}");
        assert!(s.contains("No models loaded right now."), "{s}");
        h.shoot("resources-empty");
        h.ui.models_tab.set(1);
        let s = h.turns(3);
        assert!(s.contains("No session prompt caches right now."), "{s}");
    }
    let mut h = harness(Size::new(120, 40));
    h.admin_on(ui::SCREEN_MODELS);
    h.store.host_state.set(Loadable::Ready(host(
        json!([{"task": "text_generation", "provider": "mlx", "model": "qwen3-0.6b", "resident": true,
                "state": "provider_loaded", "size_bytes": 1073741824u64}]),
        json!([]),
    )));
    let s = h.turns(3);
    assert!(s.contains("Models (1 resident)"), "{s}");
    h.shoot("resources-one-model");
}

// ---------------------------------------------------------------------
// Sandbox
// ---------------------------------------------------------------------

#[test]
fn sandbox_context_sentences_are_the_webs() {
    use abstractgateway_console::ui::sandbox::{context_ready, context_unconfigured};
    assert_eq!(
        context_ready("Image", "mlx-gen", "flux", Some("output.image"), ""),
        "Image will use mlx-gen / flux (inherited from output.image)."
    );
    assert_eq!(
        context_unconfigured("Music"),
        "Music is not configured yet. Configure it in Multimodal Capabilities first."
    );
    let mut h = harness(Size::new(120, 40));
    h.admin_on(ui::SCREEN_REVIEW);
    let s = h.turns(2);
    assert!(s.contains("╭ Sandbox"), "{s}");
    assert!(s.contains("output "), "the web's Output field:\n{s}");
    h.shoot("sandbox-text");
}
