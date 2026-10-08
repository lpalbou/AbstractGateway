//! R15 (DESIGN-TUI.md §3.8): a synthesized mouse click for EVERY OpenAI API
//! control — Copy (base URL), the Endpoint toggle, Restart, Check setup,
//! Show / Hide / Copy / New key (its [New key] [Cancel] answered by mouse),
//! the Authentication segments, the Who can connect and Requests without a
//! key run as pickers, Network, the two docs links, the example segments,
//! Copy example, and each request row's Time (the recorded request and
//! response) and Run (Open in Observer) — through the real input pipeline.
//! The meta-test enumerates `page_actions` / `log_actions` over the
//! fixture: an action without a click test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_openai.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::openai_api::{self, sha256_hex, KEY_LOGS, KEY_PAGE, MASK};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

const TOKEN: &str = "agw_r15-admin-token-0001";

fn page_view(
    ctx: &abstractgateway_console::ui::Ctx,
    cx: abstracttui::prelude::Scope,
) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    openai_api::view(cx, ctx, &t)
}

fn admin_page() -> Value {
    json!({
        "schema": "gateway_openai_api_v1", "role": "admin", "writable": true,
        "enabled": true, "running": true, "base_url": "http://127.0.0.1:18999/v1",
        "key": {"own_token": true, "user_id": "admin", "fingerprint": &sha256_hex(TOKEN.as_bytes())[..12], "allowed": true},
        "docs": {"openai_api": "https://example.test/openai-api.md", "abstractcore": "https://example.test/server.md"},
        "support": {"tested": ["GET /v1/models"], "served": ["POST /v1/responses"], "not_yet": ["n > 1"]},
        "example_model": "lmstudio/fake-model", "access": "open", "reach": "machine",
        "reach_options": [
            {"id": "machine", "label": "This machine only", "selected": true, "available": true},
            {"id": "network", "label": "Devices on my network", "selected": false, "available": true},
            {"id": "anywhere", "label": "Anywhere", "selected": false, "available": false,
             "reason": "Anywhere needs Internet on the Network page first (it asks you to confirm the risks)."}
        ],
        "open_account": "guest",
        "open_account_options": [
            {"id": "guest", "label": "Guest (models only)", "available": true, "selected": true},
            {"id": "alice", "label": "alice", "available": true, "selected": false}
        ],
        "warnings": [{"id": "listener", "tone": "warn", "text": "The gateway listens on this computer only: phones cannot reach it."}],
        "open_requests": 0
    })
}

fn logs() -> Value {
    json!({"schema": "gateway_openai_api_v1", "scope": "all", "rows": [
        {"request_id": "rid-1", "ts": "2026-10-04T02:24:03+00:00", "client": "alice", "ip": "127.0.0.1",
         "method": "POST", "path": "/v1/chat/completions", "model": "lmstudio/fake-model",
         "prompt_tokens": 12, "completion_tokens": 4, "duration_ms": 10, "status": 200,
         "observer_path": "/observer/runs/run-1"},
        {"request_id": "rid-2", "ts": "2026-10-04T02:24:04+00:00", "client": "bob", "ip": "127.0.0.1",
         "method": "GET", "path": "/v1/models", "model": null, "duration_ms": 57, "status": 403,
         "observer_path": null}
    ]})
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_openai.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn page_with(page: Value) -> r8w4::Harness {
    let mut h = harness((140, 70), Mount::Page(page_view));
    h.ui.conn_token.set(TOKEN.into());
    h.admin();
    h.store.json.set(KEY_PAGE, Loadable::Ready(page));
    h.store.json.set(KEY_LOGS, Loadable::Ready(logs()));
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_with(admin_page())
}

fn sends(h: &mut r8w4::Harness) -> Vec<(String, Value)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send { path, body, .. }) => Some((path, body)),
            _ => None,
        })
        .collect()
}

fn gets(h: &mut r8w4::Harness) -> Vec<String> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { path, .. }) => Some(path),
            _ => None,
        })
        .collect()
}

/// Click `needle` on the `nth` line holding `anchor` (the needle searched
/// right of the anchor).
fn click_on(h: &mut r8w4::Harness, anchor: &str, needle: &str) -> String {
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(anchor) && l[l.find(anchor).unwrap()..].contains(needle))
        .unwrap_or_else(|| panic!("{needle:?} on the {anchor:?} line:\n{s}"));
    let a = line.find(anchor).unwrap();
    let b = a + line[a..].find(needle).unwrap();
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn click_pair(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] button row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn offered() -> BTreeSet<&'static str> {
    let mut out = BTreeSet::new();
    for kept in [false, true] {
        for reveal in [false, true] {
            for a in openai_api::page_actions(&admin_page(), kept, reveal) {
                out.insert(a.id);
            }
        }
    }
    for r in logs()["rows"].as_array().unwrap() {
        for a in openai_api::log_actions(r) {
            out.insert(a.id);
        }
    }
    out
}

fn covered() -> BTreeSet<&'static str> {
    [
        "copy_base",
        "restart",
        "check",
        "reveal",
        "copy_key",
        "new_key",
        "network",
        "doc_openai",
        "doc_core",
        "copy_example",
        "details",
        "observer",
    ]
    .into_iter()
    .collect()
}

#[test]
fn every_offered_openai_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "OpenAI API actions without a click test: {missing:?}"
    );
}

#[test]
fn the_cards_have_the_web_words_and_mask_the_key() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "OpenAI API",
        "Let apps use your models through one OpenAI-compatible address",
        "Status",
        "● Running",
        "One address for every OpenAI-compatible app.",
        "http://127.0.0.1:18999/v1",
        "━● Endpoint",
        "Restart",
        "Check setup",
        "Connect your app",
        MASK,
        "Access",
        "Protected (API key)",
        "Open (no key)",
        "Requests without a key run as",
        "Who can connect",
        "This machine only",
        "Docs",
        "OpenAI API compatibility",
        "curl",
        "Python",
        "JavaScript",
        "Copy example",
        "Recent requests",
        "Every account's requests, newest first.",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    assert!(!s.contains(TOKEN), "the key is masked:\n{s}");
}

#[test]
fn status_card_controls() {
    let mut h = page();
    click_on(&mut h, "Base URL", "Copy");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("copied http://127.0.0.1:18999/v1")
    );
    let mut h = page();
    h.click_text("━● Endpoint");
    assert_eq!(
        sends(&mut h),
        vec![(
            "/admin/core-endpoint".to_string(),
            json!({"enabled": false})
        )]
    );
    let mut h = page();
    click_on(&mut h, "Endpoint", "Restart");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/admin/core-endpoint/restart"));
    let mut h = page();
    click_on(&mut h, "Endpoint", "Check setup");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/admin/core-endpoint/check"));
}

#[test]
fn the_key_row_shows_copies_and_makes_a_new_key() {
    let mut h = page();
    let s = click_on(&mut h, "API key", "Show");
    assert!(s.contains(TOKEN), "Show reveals it:\n{s}");
    let s = click_on(&mut h, "API key", "Hide");
    assert!(!s.contains(TOKEN), "{s}");
    click_on(&mut h, "API key", "Copy");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("API key copied to the clipboard")
    );
    let s = click_on(&mut h, "API key", "New key");
    assert!(
        s.contains("Make a new key? It replaces your gateway token"),
        "{s}"
    );
    assert!(sends(&mut h).is_empty(), "nothing before [New key]");
    click_pair(&mut h, "New key", "Cancel");
    assert!(sends(&mut h).iter().any(|(p, _)| p == "/me/token/rotate"));
}

#[test]
fn access_card_segments_and_pickers() {
    let mut h = page();
    h.click_text("Protected (API key)");
    assert_eq!(
        sends(&mut h),
        vec![(
            "/admin/core-endpoint".to_string(),
            json!({"access": "token"})
        )]
    );
    // Who can connect: open the picker, ↓ to the network, Enter.
    let mut h = page();
    let s = h.click_text("This machine only");
    assert!(s.contains("Devices on my network"), "the popup:\n{s}");
    h.key(b"\x1b[B");
    h.key(b"\r");
    assert_eq!(
        sends(&mut h),
        vec![(
            "/admin/core-endpoint".to_string(),
            json!({"reach": "network"})
        )]
    );
    // Requests without a key run as.
    let mut h = page();
    h.click_text("Guest (models only)");
    h.key(b"\x1b[B");
    h.key(b"\r");
    assert_eq!(
        sends(&mut h),
        vec![(
            "/admin/core-endpoint".to_string(),
            json!({"open_account": "alice"})
        )]
    );
    // The listener warning's Network button.
    let mut h = page();
    let s = h.turns(1);
    let y = s
        .lines()
        .position(|l| l.trim() == "Network")
        .unwrap_or_else(|| panic!("Network button:\n{s}"));
    let x = s.lines().nth(y).unwrap().find("Network").unwrap() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert_eq!(
        h.ui.screen.get_untracked(),
        abstractgateway_console::ui::SCREEN_NETWORK
    );
}

#[test]
fn docs_links_examples_and_copy_example() {
    let mut h = page();
    h.click_text("OpenAI API compatibility");
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(n.contains("https://example.test/openai-api.md"), "{n}");
    let mut h = page();
    h.click_text("AbstractCore server");
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(n.contains("https://example.test/server.md"), "{n}");
    let mut h = page();
    let s = h.click_text("Python");
    assert!(s.contains("from openai import OpenAI"), "{s}");
    let s = h.click_text("JavaScript");
    assert!(s.contains("import OpenAI from \"openai\";"), "{s}");
    h.click_text("Copy example");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("example copied to the clipboard")
    );
}

#[test]
fn a_request_opens_its_record_and_observer() {
    let mut h = page();
    let s = h.turns(1);
    let time_row = s
        .lines()
        .find(|l| l.contains("lmstudio/fake-model") && l.contains("200"))
        .unwrap_or_else(|| panic!("{s}"))
        .to_string();
    let time = time_row.split_whitespace().next().unwrap().to_string();
    let s = h.click_text(&time);
    assert!(s.contains(&format!("Request {time}")), "the record:\n{s}");
    assert!(gets(&mut h).contains(&"/openai-api/logs/rid-1".to_string()));
    let s = click_pair(&mut h, "Close", "Open in Observer");
    assert!(!s.contains(&format!("Request {time}")), "{s}");
    // Run → Open in Observer (no display in the harness: copied, said).
    click_on(&mut h, "lmstudio/fake-model", "Open");
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(n.contains("/observer/runs/run-1"), "{n}");
}

#[test]
fn hovering_restart_says_the_web_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find("Restart").map(|c| (i, l[..c].chars().count())))
        .expect("Restart");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("End open requests and keep serving  (x)"), "{s}");
}

#[test]
fn the_keyboard_reaches_the_controls() {
    let mut h = page();
    h.key(b"v");
    assert!(h.turns(1).contains(TOKEN));
    h.key(b"e");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, b)| p == "/admin/core-endpoint" && b == &json!({"enabled": false})));
}

#[test]
fn a_user_sees_no_access_card() {
    let mut p = admin_page();
    p["role"] = json!("user");
    let mut h = page_with(p);
    let s = h.turns(2);
    assert!(s.contains("Only an admin can start or stop it."), "{s}");
    assert!(!s.contains("Who can connect"), "{s}");
    assert!(!s.contains("Endpoint"), "{s}");
}

#[test]
fn fits_at_80x24() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.ui.conn_token.set(TOKEN.into());
    h.admin();
    h.store.json.set(KEY_PAGE, Loadable::Ready(admin_page()));
    h.store.json.set(KEY_LOGS, Loadable::Ready(logs()));
    let s = h.turns(3);
    assert!(s.contains("Status") && s.contains("Recent requests"), "{s}");
    h.assert_fits();
}

#[test]
fn the_words_are_the_webs() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(openai_api::ENDPOINT_TIP, w("endpoint_tip"));
    assert_eq!(openai_api::RESTART_TIP, w("restart_tip"));
    assert_eq!(openai_api::CHECK_TIP, w("check_tip"));
    assert_eq!(openai_api::SHOW_KEY_TIP, w("show_key_tip"));
    assert_eq!(openai_api::HIDE_KEY_TIP, w("hide_key_tip"));
    assert_eq!(openai_api::NOT_IN_A_RUN, w("not_in_a_run"));
    let acts = openai_api::page_actions(&admin_page(), true, false);
    let label = |id: &str| acts.iter().find(|a| a.id == id).unwrap().label.clone();
    assert_eq!(label("restart"), w("restart"));
    assert_eq!(label("check"), w("check"));
    assert_eq!(label("new_key"), w("new_key"));
    assert_eq!(label("copy_example"), w("copy_example"));
    assert_eq!(label("copy_base"), w("copy"));
    assert_eq!(label("doc_openai"), w("doc_openai"));
    assert_eq!(label("doc_core"), w("doc_core"));
    for (i, (_, l)) in openai_api::SNIPPETS.iter().enumerate() {
        assert_eq!(*l, w(&format!("snippet_{i}")));
    }
    for (i, (_, l, text)) in openai_api::AUTH.iter().enumerate() {
        assert_eq!(*l, w(&format!("auth_{i}_label")));
        assert_eq!(*text, w(&format!("auth_{i}_text")));
    }
    // The New key question: the web's, the browser's words made the console's.
    assert_eq!(
        openai_api::NEW_KEY_SENTENCE,
        w("new_key_confirm").replace(
            "this browser keeps it for this page",
            "this console keeps it for this session"
        )
    );
}
