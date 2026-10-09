//! R15 (DESIGN-TUI.md §3.8): a synthesized mouse click for EVERY OpenAI API
//! control — Copy (base URL), the Endpoint toggle, Restart, Check setup,
//! New key (its Name form: [Make key] [Cancel]), the key shown (straight to
//! the clipboard by OSC 52; Copy -> "Copied", Hide; kept across a resize),
//! each key's Reveal and Copy (the owner's key again; refused ones say why),
//! each key's Revoke (its [Revoke] [Cancel] answered by mouse), the admin's
//! "API keys can be revealed by their owner" toggle,
//! the Authentication segments, the Who can connect and Requests without a
//! key run as pickers, Network, the two docs links, the example segments,
//! Copy example, and each request row's Time (the recorded request and
//! response) and Run (Open in Observer) — through the real input pipeline.
//! The meta-test enumerates `page_actions` / `log_actions` over the
//! fixture (and `key_actions` over the keys): an action without a click
//! test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_openai.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::openai_api::{
    self, sha256_hex, KEY_KEYS, KEY_LOGS, KEY_NEW_KEY, KEY_PAGE, KEY_REVEAL, KEY_REVEAL_COPY,
    KEY_SHOWN, MASK,
};
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
        "key": {"own_token": true, "user_id": "admin", "named_keys": true, "fingerprint": &sha256_hex(TOKEN.as_bytes())[..12], "allowed": true},
        "docs": {"openai_api": "https://example.test/openai-api.md", "abstractcore": "https://example.test/server.md"},
        "support": {"tested": ["GET /v1/models"], "served": ["POST /v1/responses"], "not_yet": ["n > 1"]},
        "example_model": "lmstudio/fake-model", "access": "open", "reach": "machine", "owner_reveal": true,
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

const NEW_KEY: &str = "sk-agw-r15-fresh-named-key-0001";

fn keys() -> Value {
    json!({"schema": "gateway_openai_api_v1", "account": "admin", "keys": [
        {"label": "laptop Cursor", "fingerprint": "a1b2c3d4e5f6", "created_at": "2026-10-08T16:00:00+00:00",
         "created_by": "admin", "last_used_at": "2026-10-08T16:30:00+00:00", "last_client": "192.168.1.20",
         "revealable": true},
        {"label": "home assistant", "fingerprint": "0f1e2d3c4b5a", "created_at": "2026-10-08T16:05:00+00:00",
         "created_by": "admin", "last_used_at": null, "last_client": null, "revealable": false}
    ]})
}

fn logs() -> Value {
    json!({"schema": "gateway_openai_api_v1", "scope": "all", "rows": [
        {"request_id": "rid-1", "ts": "2026-10-04T02:24:03+00:00", "client": "alice", "ip": "127.0.0.1",
         "key_label": "laptop Cursor", "key_fingerprint": "a1b2c3d4e5f6",
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
    h.store.json.set(KEY_KEYS, Loadable::Ready(keys()));
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_with(admin_page())
}

fn methods(h: &mut r8w4::Harness) -> Vec<(String, String)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send { method, path, .. }) => Some((method, path)),
            _ => None,
        })
        .collect()
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
    for made in [false, true] {
        for a in openai_api::page_actions(&admin_page(), made) {
            out.insert(a.id);
        }
    }
    for k in keys()["keys"].as_array().unwrap() {
        for a in openai_api::key_actions(k) {
            out.insert(a.id);
        }
        for copied in [false, true] {
            for a in openai_api::own_key_actions(&admin_page(), k, copied) {
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
        "new_key",
        "copy_made",
        "made_done",
        "revoke",
        "reveal",
        "copy_key",
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
        "API keys",
        "One key can serve every app. Separate keys are optional: make one per app only to revoke an",
        "New key",
        "Your API keys",
        "laptop Cursor",
        "from 192.168.1.20",
        "a1b2c3d4e5f6",
        "Never used",
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
    assert!(!s.contains(TOKEN), "the gateway token is never shown:\n{s}");
    assert!(!s.contains(MASK), "no token row:\n{s}");
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
fn new_key_asks_a_name_then_shows_the_key_once() {
    let mut h = page();
    let s = click_on(&mut h, "API keys", "New key");
    assert!(s.contains("Make key"), "the Name form:\n{s}");
    assert!(s.contains("Name it after the app that will use it"), "{s}");
    h.type_text("phone app");
    click_pair(&mut h, "Make key", "Cancel");
    assert_eq!(
        sends(&mut h),
        vec![("/me/openai-keys".to_string(), json!({"label": "phone app"}))]
    );
    // The answer: the key, shown once with Copy and Done.
    h.store.json.set_write(
        KEY_NEW_KEY,
        Some(WriteState::Done(
            json!({"key": NEW_KEY, "item": {"label": "phone app", "fingerprint": "999999999999"}}),
        )),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Key “phone app” made and copied to the clipboard."),
        "{s}"
    );
    assert!(
        s.contains("Keep it somewhere safe: this gateway shows a key only once."),
        "the item is not revealable:\n{s}"
    );
    assert!(s.contains(NEW_KEY), "shown, in clear:\n{s}");
    // Straight to the clipboard: the engine's OSC 52 frame carries it.
    assert!(clipboard(&h).contains(&b64(NEW_KEY)), "OSC 52 with the key");
    assert!(
        s.contains(" Copied   Hide"),
        "the Copy button says Copied:\n{s}"
    );
    // Copy again (the button keeps its place; it says Copied for 2 s).
    h.term.take_bytes();
    h.store.notice.set(None);
    click_pair(&mut h, "Copied", "Hide");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("API key copied to the clipboard")
    );
    h.turns(2);
    assert!(clipboard(&h).contains(&b64(NEW_KEY)), "Copy: OSC 52 again");
    click_pair(&mut h, "Hide", "Copied");
    let s = h.turns(2);
    assert!(!s.contains(NEW_KEY), "Hide forgets it:\n{s}");
}

/// The OSC 52 payloads the engine wrote (base64 of what was copied).
fn clipboard(h: &r8w4::Harness) -> String {
    String::from_utf8_lossy(h.term.bytes()).to_string()
}

fn b64(text: &str) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in text.as_bytes().chunks(3) {
        let n = (c[0] as u32) << 16
            | (*c.get(1).unwrap_or(&0) as u32) << 8
            | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                out.push(T[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn made(h: &mut r8w4::Harness, revealable: bool) {
    click_on(h, "API keys", "New key");
    h.type_text("phone app");
    click_pair(h, "Make key", "Cancel");
    h.sent();
    h.store.json.set_write(
        KEY_NEW_KEY,
        Some(WriteState::Done(json!({"key": NEW_KEY,
            "item": {"label": "phone app", "fingerprint": "999999999999", "revealable": revealable}}))),
    );
    h.turns(3);
}

#[test]
fn a_new_key_survives_a_resize_and_says_it_can_be_revealed() {
    let mut h = page();
    made(&mut h, true);
    let s = h.turns(1);
    assert!(s.contains(NEW_KEY), "{s}");
    assert!(
        s.contains("You can reveal it again any time under Your API keys."),
        "{s}"
    );
    // The operator's case: resize the window (smaller, then back).
    h.term.push_resize(abstracttui::base::Size::new(100, 40));
    let s = h.turns(3);
    assert!(s.contains(NEW_KEY), "kept at 100x40:\n{s}");
    h.term.push_resize(abstracttui::base::Size::new(161, 70));
    let s = h.turns(3);
    assert!(s.contains(NEW_KEY), "kept after the resize back:\n{s}");
    assert!(h.store.json.get_untracked(KEY_SHOWN).ready().is_some());
    // The examples carry it (in clear) instead of <your key>.
    let s = h.wheel_down(30);
    assert!(
        s.contains(&format!("Bearer {NEW_KEY}")),
        "the example:\n{s}"
    );
    assert!(s.contains("The examples use your key “phone app”."), "{s}");
}

#[test]
fn the_examples_say_your_key_until_one_is_shown() {
    let mut h = page_with({
        let mut p = admin_page();
        p["access"] = json!("token");
        p
    });
    let s = h.wheel_down(30);
    assert!(s.contains("Bearer <your key>"), "{s}");
    assert!(
        s.contains("<your key> is filled in when you make or reveal a key under Your API keys."),
        "{s}"
    );
}

#[test]
fn reveal_shows_the_owners_key() {
    let mut h = page();
    click_on(&mut h, "laptop Cursor", "⦿");
    assert_eq!(
        methods(&mut h),
        vec![(
            "POST".to_string(),
            "/me/openai-keys/a1b2c3d4e5f6/reveal".to_string()
        )]
    );
    h.store.json.set_write(
        KEY_REVEAL,
        Some(WriteState::Done(json!({"key": "sk-agw-revealed-0001",
            "item": {"label": "laptop Cursor", "fingerprint": "a1b2c3d4e5f6", "revealable": true}}))),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Key “laptop Cursor”. Each reveal is noted in the audit log."),
        "{s}"
    );
    assert!(s.contains("sk-agw-revealed-0001"), "{s}");
}

#[test]
fn copy_on_a_row_puts_the_key_on_the_clipboard() {
    let mut h = page();
    click_on(&mut h, "laptop Cursor", "Copy");
    assert_eq!(
        methods(&mut h),
        vec![(
            "POST".to_string(),
            "/me/openai-keys/a1b2c3d4e5f6/reveal".to_string()
        )]
    );
    h.term.take_bytes();
    h.store.json.set_write(
        KEY_REVEAL_COPY,
        Some(WriteState::Done(json!({"key": "sk-agw-revealed-0002",
            "item": {"label": "laptop Cursor", "fingerprint": "a1b2c3d4e5f6", "revealable": true}}))),
    );
    let s = h.turns(3);
    assert!(
        clipboard(&h).contains(&b64("sk-agw-revealed-0002")),
        "OSC 52"
    );
    assert!(
        !s.contains("sk-agw-revealed-0002"),
        "Copy does not show it:\n{s}"
    );
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("API key “laptop Cursor” copied to the clipboard")
    );
    let line = s
        .lines()
        .find(|l| l.contains("laptop Cursor") && l.contains("Revoke"))
        .unwrap_or_else(|| panic!("{s}"));
    assert!(line.contains("Copied"), "the row's Copy says Copied:\n{s}");
}

#[test]
fn a_key_that_cannot_be_revealed_says_why() {
    let mut h = page();
    click_on(&mut h, "home assistant", "⦿");
    assert!(methods(&mut h).is_empty(), "nothing sent");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(openai_api::NOT_REVEALABLE_WHY)
    );
    let mut p = admin_page();
    p["owner_reveal"] = json!(false);
    let mut h = page_with(p);
    click_on(&mut h, "laptop Cursor", "Copy");
    assert!(methods(&mut h).is_empty(), "nothing sent");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(openai_api::REVEAL_OFF_WHY)
    );
}

#[test]
fn the_admin_turns_owner_reveal_off() {
    let mut h = page();
    h.click_text(openai_api::OWNER_REVEAL_LABEL);
    assert_eq!(
        sends(&mut h),
        vec![(
            "/admin/core-endpoint".to_string(),
            json!({"owner_reveal": false})
        )]
    );
    let mut answered = admin_page();
    answered["owner_reveal"] = json!(false);
    h.store
        .json
        .set_write(openai_api::KEY_CHANGE, Some(WriteState::Done(answered)));
    let s = h.turns(3);
    assert!(
        s.contains("Saved: API keys are shown once; stored keys were erased."),
        "{s}"
    );
}

#[test]
fn new_key_without_a_name_says_the_web_sentence() {
    let mut h = page();
    click_on(&mut h, "API keys", "New key");
    let s = click_pair(&mut h, "Make key", "Cancel");
    assert!(
        s.contains("Give the key a name, for example the app that will use it."),
        "{s}"
    );
    assert!(sends(&mut h).is_empty(), "nothing sent without a name");
}

#[test]
fn new_key_cancel_with_a_typed_name_asks_before_dropping_it() {
    let mut h = page();
    click_on(&mut h, "API keys", "New key");
    h.type_text("draft");
    let s = click_pair(&mut h, "Cancel", "Make key");
    assert!(s.contains("Discard changes?"), "{s}");
    assert!(sends(&mut h).is_empty());
}

#[test]
fn revoke_asks_then_deletes_the_key() {
    let mut h = page();
    let s = click_on(&mut h, "laptop Cursor", "Revoke");
    assert!(
        s.contains("Revoke “laptop Cursor”? Apps using it stop working at once."),
        "{s}"
    );
    assert!(sends(&mut h).is_empty(), "nothing before [Revoke]");
    click_pair(&mut h, "Revoke", "Cancel");
    assert_eq!(
        methods(&mut h),
        vec![(
            "DELETE".to_string(),
            "/me/openai-keys/a1b2c3d4e5f6".to_string()
        )]
    );
    h.store.json.set_write(
        openai_api::KEY_REVOKE,
        Some(WriteState::Done(
            json!({"revoked": {"label": "laptop Cursor", "fingerprint": "a1b2c3d4e5f6"}}),
        )),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Revoked “laptop Cursor”: apps using it are refused from now on."),
        "{s}"
    );
}

#[test]
fn enter_on_the_revoke_question_keeps_the_key() {
    let mut h = page();
    let s = click_on(&mut h, "laptop Cursor", "Revoke");
    assert!(s.contains("Revoke “laptop Cursor”?"), "{s}");
    h.key(b"\r");
    h.turns(2);
    assert!(methods(&mut h).is_empty(), "Enter on Cancel keeps it");
}

#[test]
fn no_keys_says_the_web_sentence() {
    let mut h = page();
    h.store
        .json
        .set(KEY_KEYS, Loadable::Ready(json!({"keys": []})));
    let s = h.turns(3);
    assert!(
        s.contains("No API keys yet. Make one with New key: one key can serve every app."),
        "{s}"
    );
}

#[test]
fn the_operator_token_has_no_named_keys() {
    let mut p = admin_page();
    p["key"] =
        json!({"own_token": false, "user_id": "admin", "named_keys": false, "allowed": true});
    let mut h = page_with(p);
    let s = h.turns(3);
    assert!(s.contains("The gateway admin token"), "{s}");
    assert!(
        s.contains("Named keys belong to an account: sign in with one to make them."),
        "{s}"
    );
    assert!(
        !s.contains("New key") && !s.contains("Your API keys"),
        "{s}"
    );
}

#[test]
fn the_keys_are_read_once_the_page_says_so() {
    let mut h = harness((140, 70), Mount::Page(page_view));
    h.ui.conn_token.set(TOKEN.into());
    h.admin();
    h.store.json.set(KEY_PAGE, Loadable::Ready(admin_page()));
    h.turns(3);
    assert!(gets(&mut h).contains(&"/me/openai-keys".to_string()));
}

#[test]
fn a_request_row_names_its_key() {
    let mut h = page();
    let s = h.turns(2);
    let rows: Vec<&str> = s.lines().collect();
    let i = rows
        .iter()
        .position(|l| l.contains("lmstudio/fake-model") && l.contains("200"))
        .unwrap_or_else(|| panic!("{s}"));
    assert!(
        rows[i..(i + 2).min(rows.len())]
            .iter()
            .any(|l| l.contains("laptop Cursor")),
        "the key under the client:\n{s}"
    );
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
        .position(|l| l.trim().trim_end_matches(['┃', '│']).trim() == "Network")
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
    h.click_text("Python");
    // The example is below the fold since the Access card has the owner-reveal row.
    let s = h.wheel_down(30);
    assert!(s.contains("from openai import OpenAI"), "{s}");
    h.click_text("JavaScript");
    let s = h.wheel_down(30);
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
    let s = h.key(b"n");
    assert!(
        s.contains("Make key") && s.contains("Name it after the app"),
        "n = New key:\n{s}"
    );
    let mut h = page();
    h.key(b"e");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, b)| p == "/admin/core-endpoint" && b == &json!({"enabled": false})));
    // v / y on the selected key (the first row): Reveal, Copy.
    for k in [b"v", b"y"] {
        let mut h = page();
        h.key(k);
        assert_eq!(
            methods(&mut h),
            vec![(
                "POST".to_string(),
                "/me/openai-keys/a1b2c3d4e5f6/reveal".to_string()
            )]
        );
    }
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
    assert_eq!(openai_api::NOT_IN_A_RUN, w("not_in_a_run"));
    let acts = openai_api::page_actions(&admin_page(), true);
    let label = |id: &str| acts.iter().find(|a| a.id == id).unwrap().label.clone();
    assert_eq!(label("restart"), w("restart"));
    assert_eq!(label("check"), w("check"));
    assert_eq!(label("new_key"), w("new_key"));
    assert_eq!(label("copy_example"), w("copy_example"));
    assert_eq!(label("copy_base"), w("copy"));
    assert_eq!(label("doc_openai"), w("doc_openai"));
    assert_eq!(label("doc_core"), w("doc_core"));
    assert_eq!(label("copy_made"), w("copy"));
    assert_eq!(label("made_done"), w("made_done"));
    assert_eq!(openai_api::HIDE, w("made_done"));
    assert_eq!(openai_api::COPIED, w("copied"));
    assert_eq!(openai_api::REVEAL_AGAIN, w("reveal_again"));
    assert_eq!(openai_api::SHOWN_ONCE, w("shown_once"));
    assert_eq!(openai_api::REVEAL_TIP, w("reveal_tip"));
    assert_eq!(openai_api::COPY_KEY_TIP, w("copy_key_tip"));
    assert_eq!(openai_api::REVEAL_OFF_WHY, w("reveal_off_why"));
    assert_eq!(openai_api::NOT_REVEALABLE_WHY, w("not_revealable_why"));
    assert_eq!(openai_api::DOCS_NOTE, w("docs_note"));
    assert_eq!(openai_api::SNIPPET_NOTE_EMPTY, w("snippet_note_empty"));
    assert_eq!(openai_api::KEY_PLACEHOLDER, w("key_placeholder"));
    assert_eq!(openai_api::OWNER_REVEAL_LABEL, w("owner_reveal_label"));
    assert_eq!(openai_api::OWNER_REVEAL_HELP, w("owner_reveal_help"));
    assert_eq!(
        openai_api::revealed_sentence("X"),
        w("revealed_sentence").replace("{label}", "X")
    );
    assert_eq!(
        openai_api::snippet_note_key("X"),
        w("snippet_note_key").replace("{label}", "X")
    );
    let mut on = admin_page();
    assert_eq!(
        openai_api::saved_text("owner_reveal", &on),
        w("saved_owner_reveal_on")
    );
    on["owner_reveal"] = json!(false);
    assert_eq!(
        openai_api::saved_text("owner_reveal", &on),
        w("saved_owner_reveal_off")
    );
    let own = openai_api::own_key_actions(&admin_page(), &keys()["keys"][0], false);
    let own_tip = |id: &str| own.iter().find(|a| a.id == id).unwrap().tooltip.clone();
    assert_eq!(own_tip("reveal").as_deref(), Some(w("reveal_tip").as_str()));
    assert_eq!(
        own_tip("copy_key").as_deref(),
        Some(w("copy_key_tip").as_str())
    );
    let refused = openai_api::own_key_actions(&admin_page(), &keys()["keys"][1], false);
    assert_eq!(
        refused.iter().find(|a| a.id == "reveal").unwrap().enabled,
        Err(w("not_revealable_why"))
    );
    let tip = |id: &str| acts.iter().find(|a| a.id == id).unwrap().tooltip.clone();
    assert_eq!(tip("new_key").as_deref(), Some(w("keys_note").as_str()));
    assert_eq!(
        tip("copy_example").as_deref(),
        Some(w("copy_example_tip").as_str())
    );
    assert_eq!(
        tip("copy_made").as_deref(),
        Some(w("copy_made_tip").as_str())
    );
    assert_eq!(openai_api::KEYS_NOTE, w("keys_note"));
    assert_eq!(openai_api::ADMIN_TOKEN_LABEL, w("admin_token_label"));
    assert_eq!(openai_api::ADMIN_TOKEN_NOTE, w("admin_token_note"));
    assert_eq!(openai_api::NAME_LABEL, w("name_label"));
    assert_eq!(openai_api::NAME_PLACEHOLDER, w("name_placeholder"));
    assert_eq!(openai_api::NAME_HELP, w("name_help"));
    assert_eq!(openai_api::MAKE_KEY, w("make_key"));
    assert_eq!(openai_api::NAME_REQUIRED, w("name_required"));
    assert_eq!(openai_api::KEYS_EMPTY, w("keys_empty"));
    assert_eq!(openai_api::KEYS_TABLE, w("keys_table"));
    assert_eq!(openai_api::REVOKE_TIP, w("revoke_tip"));
    assert_eq!(openai_api::COPY_MADE_TIP, w("copy_made_tip"));
    assert_eq!(
        openai_api::made_sentence("X"),
        w("made_sentence").replace("{label}", "X")
    );
    assert_eq!(
        openai_api::revoke_sentence("X"),
        w("revoke_confirm").replace("{label}", "X")
    );
    assert_eq!(
        openai_api::revoked_sentence("X"),
        w("revoked").replace("{label}", "X")
    );
    assert_eq!(
        openai_api::account_lead("bob"),
        w("account_lead").replace("{id}", "bob")
    );
    assert_eq!(
        openai_api::account_keys_empty("bob"),
        w("account_keys_empty").replace("{id}", "bob")
    );
    assert_eq!(
        openai_api::account_revoke_sentence("X", "bob"),
        w("account_revoke_confirm")
            .replace("{label}", "X")
            .replace("{id}", "bob")
    );
    let k = &keys()["keys"][0];
    let revoke = openai_api::key_actions(k)
        .into_iter()
        .find(|a| a.id == "revoke")
        .unwrap();
    assert_eq!(revoke.label, w("revoke"));
    assert_eq!(revoke.tooltip.as_deref(), Some(w("revoke_tip").as_str()));
    for (i, (_, l)) in openai_api::SNIPPETS.iter().enumerate() {
        assert_eq!(*l, w(&format!("snippet_{i}")));
    }
    for (i, (_, l, text)) in openai_api::AUTH.iter().enumerate() {
        assert_eq!(*l, w(&format!("auth_{i}_label")));
        assert_eq!(*text, w(&format!("auth_{i}_text")));
    }
}

/// The operator's report (2026-10-09): "I resized the window and it lost the
/// key". In the real shell (root mount) a resize across the rail threshold
/// and a trip to another page rebuild the OpenAI page: the shown key lives in
/// the store, so it is still there until Hide.
#[test]
fn the_shell_keeps_a_new_key_across_resizes_and_page_changes() {
    let mut h = harness((140, 50), Mount::Root);
    h.ui.conn_token.set(TOKEN.into());
    h.admin();
    h.store.json.set(KEY_PAGE, Loadable::Ready(admin_page()));
    h.store.json.set(KEY_LOGS, Loadable::Ready(logs()));
    h.store.json.set(KEY_KEYS, Loadable::Ready(keys()));
    h.ui.screen.set(abstractgateway_console::ui::SCREEN_OPENAI);
    h.turns(3);
    h.store.json.set_write(
        KEY_NEW_KEY,
        Some(WriteState::Done(json!({"key": NEW_KEY,
            "item": {"label": "phone app", "fingerprint": "999999999999", "revealable": true}}))),
    );
    let s = h.turns(3);
    assert!(s.contains(NEW_KEY), "{s}");
    // Narrow (no rail; the key is below the fold there), then wide again.
    h.term.push_resize(abstracttui::base::Size::new(90, 30));
    h.turns(4);
    assert!(
        h.store.json.get_untracked(KEY_SHOWN).ready().is_some(),
        "kept at 90x30"
    );
    for (w, ht) in [(200, 60), (140, 50)] {
        h.term.push_resize(abstracttui::base::Size::new(w, ht));
        let s = h.turns(4);
        assert!(s.contains(NEW_KEY), "kept at {w}x{ht}:\n{s}");
    }
    h.ui.screen.set(abstractgateway_console::ui::SCREEN_NETWORK);
    h.turns(3);
    h.ui.screen.set(abstractgateway_console::ui::SCREEN_OPENAI);
    let s = h.turns(4);
    assert!(s.contains(NEW_KEY), "kept after another page:\n{s}");
    assert!(
        s.contains("Key “phone app” made and copied to the clipboard."),
        "{s}"
    );
}
