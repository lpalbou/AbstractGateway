//! R15 (DESIGN-TUI.md §3.13, §6.1): the Network page by mouse. Every
//! control — the mode segments, each address's Copy, Check again, Look up
//! my public address, Restart now, What to know, each origin's Remove, the
//! origin field + Add origin, the Trust proxies switch, the OpenAI API
//! pointer — gets a synthesized SGR click asserted on the request it sends
//! (or what it opens/says). The Internet confirmation is answered BY MOUSE.
//! The meta-test enumerates `network::page_actions` / `address_actions` /
//! `origin_actions` over the fixtures: an action without a click test is
//! RED. The words are the web's (`tests/fixtures/r15_web_wording_network.json`,
//! rebuilt by `scripts/extract_web_wording.py`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{Loadable, NetworkData};
use abstractgateway_console::ui::{self, network};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::operator::OpCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    network::view(cx, ctx, &t)
}

/// A GET /network answer: `configured` mode, a pending restart, the
/// warnings, one saved origin.
fn network(configured: &str, restart: bool) -> Value {
    json!({
        "schema": "gateway_network_v1",
        "writable": true,
        "configured": {"mode": configured, "label": match configured {
            "localhost" => "Localhost only", "lan" => "Local network", _ => "Internet"},
            "port": 8080, "source": "stored", "port_source": "stored"},
        "effective": {"mode": "localhost", "label": "Localhost only", "bind_host": "127.0.0.1",
            "port": 18811, "overridden_by_cli": false, "pinned_by_cli": false,
            "host_source": "setting", "port_source": "cli", "running": true},
        "restart_required": restart,
        "restart": {"available": true, "applies": true, "needed": restart, "port": 8080, "how": ""},
        "auth": {"user_auth": true, "ok_for_mode": true, "will_enable_user_auth": false},
        "modes": [
            {"id": "localhost", "label": "Localhost only", "selected": configured == "localhost", "allowed": true},
            {"id": "lan", "label": "Local network", "selected": configured == "lan", "allowed": true},
            {"id": "internet", "label": "Internet", "selected": configured == "internet", "allowed": true}
        ],
        "addresses": [
            {"kind": "loopback", "url": "http://127.0.0.1:18811", "reachable": true, "note": "this machine only"},
            {"kind": "lan", "url": "http://192.168.1.20:18811", "interface_label": "Wi-Fi", "reachable": true, "note": ""},
            {"kind": "tailscale", "url": "http://mac.tail1234.ts.net:18811", "reachable": false, "note": ""}
        ],
        "copy_hint": "http://127.0.0.1:18811",
        "checked_at": "2026-10-08T02:11:23.920397Z",
        "warnings": ["Traffic is plain HTTP: use it on networks you trust."],
        "reverse_proxy": {
            "allowed_origins": {"value": ["https://gw.example.com"], "source": "setting", "overridden_by_env": false,
                "effective": [], "builtin": ["http://localhost:*"], "self_origins": [], "applies": "live", "warnings": []},
            "trust_proxy": {"value": false, "source": "default", "overridden_by_env": false, "effective": false,
                "applies": "live", "loopback": true}
        }
    })
}

fn page_with(v: Value) -> r8w4::Harness {
    let mut h = harness((120, 60), Mount::Page(page_view));
    h.admin();
    h.store
        .network
        .set(Loadable::Ready(NetworkData::from_value(&v)));
    h.turns(3);
    h.sent();
    h
}

fn set_net(h: &mut r8w4::Harness, v: Value) -> String {
    h.store
        .network
        .set(Loadable::Ready(NetworkData::from_value(&v)));
    h.turns(3)
}

fn bodies(cmds: &[Cmd]) -> Vec<Value> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send { body, .. }) => Some(body.clone()),
            _ => None,
        })
        .collect()
}

fn notice(h: &r8w4::Harness) -> String {
    h.store.notice.get_untracked().unwrap_or_default()
}

/// Click `needle` on the line holding `row` (first such line).
fn click_on_row(h: &mut r8w4::Harness, row: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(row) && l.contains(needle))
        .unwrap_or_else(|| panic!("no line with {row:?} and {needle:?}:\n{screen}"));
    let at = line.find(needle).unwrap();
    let x = line[..at].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_network.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

/// (scope, action id) every fixture state offers.
fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    let mut internet = network("internet", true);
    internet["effective"]["mode"] = json!("internet");
    for v in [network("lan", false), internet] {
        let d = NetworkData::from_value(&v);
        for a in network::page_actions(&d) {
            out.insert(("page".into(), a.id));
        }
        for a in &d.addresses {
            for x in network::address_actions(a) {
                out.insert((a.kind.clone(), x.id));
            }
        }
        for (o, x) in network::origin_actions(&d) {
            out.insert((o, x.id));
        }
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("page", "restart"),
        ("page", "lookup"),
        ("page", "check"),
        ("page", "openai"),
        ("page", "add_origin"),
        ("loopback", "copy"),
        ("lan", "copy"),
        ("https://gw.example.com", "remove_origin"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_network_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Network actions without a click test: {missing:?}"
    );
    // No Copy on an address this mode does not serve (the web shows none).
    let d = NetworkData::from_value(&network("lan", false));
    let ts = d.addresses.iter().find(|a| a.kind == "tailscale").unwrap();
    assert!(network::address_actions(ts).is_empty());
}

#[test]
fn copy_buttons_copy_their_address() {
    let mut h = page_with(network("lan", false));
    click_on_row(&mut h, "http://127.0.0.1:18811", " Copy ");
    assert_eq!(notice(&h), "copied http://127.0.0.1:18811");
    click_on_row(&mut h, "http://192.168.1.20:18811", " Copy ");
    assert_eq!(notice(&h), "copied http://192.168.1.20:18811");
}

#[test]
fn the_page_buttons_send_their_requests() {
    // Check again: re-reads GET /network.
    let mut h = page_with(network("lan", false));
    h.click_text(" Check again ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::LoadNetwork)));
    // Restart now (a pending restart that can apply).
    let mut h = page_with(network("lan", true));
    let s = h.turns(1);
    assert!(
        s.contains("Restart to apply: Local network on port 8080."),
        "{s}"
    );
    h.click_text(" Restart now ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Operator(OpCmd::RestartNetwork))));
    // Look up my public address (Internet mode, no public address yet).
    let mut v = network("internet", false);
    v["effective"]["mode"] = json!("internet");
    let mut h = page_with(v);
    h.click_text(" Look up my public address ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Operator(OpCmd::LookupPublic))));
    // The OpenAI API pointer opens its page.
    let mut h = page_with(network("lan", false));
    h.click_text(" OpenAI API ");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_OPENAI);
}

#[test]
fn the_mode_segments_post_the_mode_and_internet_is_confirmed_by_mouse() {
    let mut h = page_with(network("lan", false));
    // The segments' row (the heading above also names the running mode).
    const SEGS: &str = " Localhost only   Local network   Internet ";
    click_on_row(&mut h, SEGS, " Localhost only ");
    assert_eq!(bodies(&h.sent()), vec![json!({"mode": "localhost"})]);
    h.store.json.set_write("network.mode", None);
    click_on_row(&mut h, SEGS, " Internet ");
    assert_eq!(bodies(&h.sent()), vec![json!({"mode": "internet"})]);
    // The gateway asks for the acknowledgement: the confirmation opens.
    let mut e = ApiError::new(ApiErrorKind::Http(409), "acknowledgement required");
    e.body = Some(
        json!({"reason_code": "acknowledgement_required", "mode": "internet",
        "refused_reason": "Internet mode needs your acknowledgement.",
        "warnings": ["Put a TLS proxy in front."]}),
    );
    h.store
        .json
        .set_write("network.mode", Some(WriteState::Failed(e.clone())));
    let s = h.turns(3);
    assert!(
        s.contains("Before you open the gateway to the internet"),
        "{s}"
    );
    assert!(s.contains("• Put a TLS proxy in front."), "{s}");
    // A reload under it (the re-read after the write) keeps it open.
    let s = set_net(&mut h, network("lan", false));
    assert!(
        s.contains("Before you open the gateway to the internet"),
        "survives:\n{s}"
    );
    // Keep sends nothing.
    click_on_row(&mut h, " Keep Local network ", " Keep Local network ");
    assert!(bodies(&h.sent()).is_empty());
    // Asked again; the action button acknowledges.
    h.store
        .json
        .set_write("network.mode", Some(WriteState::Failed(e)));
    h.turns(3);
    click_on_row(
        &mut h,
        " I understand, use Internet mode ",
        " I understand, use Internet mode ",
    );
    assert_eq!(
        bodies(&h.sent()),
        vec![json!({"mode": "internet", "acknowledge_internet": true})]
    );
}

#[test]
fn the_internet_confirm_opens_on_keep() {
    // Destructive-class confirm: the focus starts on Keep, so Enter keeps.
    let mut h = page_with(network("lan", false));
    let mut e = ApiError::new(ApiErrorKind::Http(409), "ack");
    e.body = Some(json!({"reason_code": "acknowledgement_required", "mode": "internet"}));
    h.store
        .json
        .set_write("network.mode", Some(WriteState::Failed(e)));
    h.turns(3);
    h.key(b"\r");
    assert!(bodies(&h.sent()).is_empty(), "Enter on the default keeps");
    let s = h.turns(2);
    assert!(!s.contains("Before you open the gateway"), "closed:\n{s}");
}

#[test]
fn origins_remove_add_and_the_trust_switch_by_mouse() {
    let mut h = page_with(network("lan", false));
    click_on_row(&mut h, "https://gw.example.com", " Remove ");
    assert_eq!(bodies(&h.sent()), vec![json!({"allowed_origins": []})]);
    h.store.json.set_write("network.proxy", None);
    // Add origin with nothing typed: the web's sentence, nothing sent.
    let s = h.click_text(" Add origin ");
    assert!(
        s.contains("Type an origin, for example https://gateway.example.com."),
        "{s}"
    );
    assert!(bodies(&h.sent()).is_empty());
    // Click the field, type, click Add origin.
    h.click_text("https://gateway.example.com ");
    h.type_text("https://tunnel.example.org");
    h.click_text(" Add origin ");
    assert_eq!(
        bodies(&h.sent()),
        vec![json!({"allowed_origins": ["https://gw.example.com", "https://tunnel.example.org"]})]
    );
    h.store.json.set_write("network.proxy", None);
    h.turns(2);
    // The switch.
    h.click_text("●─ Trust proxies on other machines");
    assert_eq!(bodies(&h.sent()), vec![json!({"trust_proxy": true})]);
}

#[test]
fn what_to_know_opens_and_closes_by_click_and_key() {
    let mut h = page_with(network("lan", false));
    let s = h.turns(1);
    assert!(
        !s.contains("Traffic is plain HTTP"),
        "closed by default:\n{s}"
    );
    let s = h.click_text("What to know about Local network (1)");
    assert!(s.contains("• Traffic is plain HTTP"), "{s}");
    let s = h.key(b"w");
    assert!(!s.contains("Traffic is plain HTTP"), "{s}");
}

#[test]
fn hovering_copy_names_the_address_and_keyboard_copies() {
    let mut h = page_with(network("lan", false));
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.find("http://127.0.0.1:18811")
                .and(l.rfind(" Copy "))
                .map(|c| (i, l[..c].chars().count() + 2))
        })
        .expect("the loopback Copy");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Copy http://127.0.0.1:18811  (c)"), "{s}");
    // Keyboard only: Tab from the address table enters the selected row's
    // Copy; Enter presses it.
    let mut h = page_with(network("lan", false));
    h.key(b"\x1b[B");
    h.key(b"\t");
    h.key(b"\r");
    assert_eq!(notice(&h), "copied http://192.168.1.20:18811");
}

#[test]
fn the_network_words_are_the_webs() {
    let fx = fixture();
    for (id, want) in fx["modes"].as_object().unwrap() {
        assert_eq!(network::mode_text(id), want.as_str().unwrap(), "{id}");
    }
    let d = NetworkData::from_value(&network("lan", false));
    for a in &d.addresses {
        let base = fx["kinds"][a.kind.as_str()].as_str().unwrap();
        assert!(network::kind_label(a).starts_with(base), "{}", a.kind);
        for x in network::address_actions(a) {
            assert_eq!(x.label, fx["buttons"]["copy"].as_str().unwrap());
            assert_eq!(
                x.tooltip.as_deref(),
                Some(
                    fx["aria"]["copy"]
                        .as_str()
                        .unwrap()
                        .replace("{url}", &a.url)
                        .as_str()
                )
            );
        }
        let pill = network::reach_pill(a);
        assert!(
            fx["pills"].as_object().unwrap().values().any(|v| v == pill),
            "{pill}"
        );
    }
    for (o, x) in network::origin_actions(&d) {
        assert_eq!(
            x.tooltip.as_deref(),
            Some(
                fx["aria"]["remove"]
                    .as_str()
                    .unwrap()
                    .replace("{origin}", &o)
                    .as_str()
            )
        );
    }
    let mut v = network("internet", true);
    v["effective"]["mode"] = json!("internet");
    let buttons = fx["buttons"].as_object().unwrap();
    for a in network::page_actions(&NetworkData::from_value(&v)) {
        assert!(
            buttons.values().any(|b| b == a.label.as_str()),
            "{} is not a web button label",
            a.label
        );
    }
    let text = &fx["text"];
    assert_eq!(network::NON_ADMIN_MODE, text["non_admin_mode"]);
    assert_eq!(network::NON_ADMIN_PROXY, text["non_admin_proxy"]);
    assert_eq!(network::EMPTY_ORIGIN, text["empty_origin"]);
    assert_eq!(network::INTERNET_CONFIRM, text["confirm"]);
    assert_eq!(network::INTERNET_GO, buttons["internet_go"]);
    assert_eq!(network::TRUST_LABEL, text["trust"]);
    assert_eq!(network::TRUST_TEXT, text["trust_text"]);
    assert_eq!(network::TRUST_DANGER, text["trust_danger"]);
    assert_eq!(
        network::keep_label(&d),
        buttons["keep"]
            .as_str()
            .unwrap()
            .replace("{mode}", "Local network")
    );
    // The rendered page carries the headings and sentences verbatim.
    let mut h = page_with(network("lan", false));
    let s = h.turns(2);
    for k in [
        "who",
        "running",
        "addresses",
        "addresses_sub",
        "other",
        "other_sub",
        "origins",
        "client",
        "applies",
    ] {
        let want = text[k].as_str().unwrap();
        assert!(s.contains(want), "{k}: {want:?} not on the page:\n{s}");
    }
}
