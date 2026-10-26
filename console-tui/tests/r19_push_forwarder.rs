//! My email → Notifications → Push forwarder (admins; framework backlog 1049),
//! the web's card in the terminal: the page reads `GET /push/settings` when an
//! admin opens it, says the address in words ("Built-in forwarder" | "Off" |
//! the URL, "set by the launch flag for this run" when the flag wins), and
//! Edit offers the three choices; Save sends `PUT /push/settings {mode, url?}`
//! and says what the forwarder answered, or the gateway's refusal.

mod r2email;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::email::MyEmail;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{ConnPhase, Identity, Loadable};
use abstractgateway_console::ui::my_email::{
    self, forwarder, probe_words, put_body, BUILT_IN, FLAG_WINS, FORWARDER_HELP, K_ACCOUNTS,
    K_FORWARDER, K_FORWARDER_WRITE, NONE_BUILT_IN,
};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use abstracttui::prelude::*;
use r2email::{harness, Harness};
use serde_json::{json, Value};

fn settings() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": true, "email_available": true,
        "address": "", "registered_address": "admin@example.org",
        "mailbox": {"state": "not_connected", "address": "", "provider": "", "reason": null},
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": true, "active": false},
        "notifications": {"job_failed": true, "approval_needed": true},
        "oauth_providers": []
    })
}

fn fwd(mode: &str, url: Option<&str>, effective: Option<&str>, source: &str) -> Value {
    json!({"forwarder": {"mode": mode, "url": url, "effective": effective, "source": source, "packaged": null}})
}

fn page(admin: bool) -> Harness {
    let mut h = harness(Size::new(150, 70));
    h.admin();
    if !admin {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": "alice", "tenant_id": "default", "roles": ["user"], "admin": false},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        h.store.conn.set(ConnPhase::Connected(id));
    }
    h.turns(2);
    let (cx, ctx) = h.root.borrow().clone().expect("root");
    my_email::open(cx, &ctx);
    h.turns(2);
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(&settings())));
    h.store
        .json
        .set(K_ACCOUNTS, Loadable::Ready(json!({"items": []})));
    h.turns(4);
    h
}

fn scroll_to(h: &mut Harness, needle: &str) -> String {
    for _ in 0..30 {
        let s = h.turns(1);
        if s.contains(needle) {
            return s;
        }
        h.wheel_down(3);
    }
    panic!("{needle:?} never on screen:\n{}", h.turns(1));
}

#[test]
fn words_follow_the_web() {
    let w = |v: Value| forwarder(&v).expect("a forwarder").words();
    assert_eq!(
        w(fwd("default", None, None, "packaged")),
        (BUILT_IN.to_string(), NONE_BUILT_IN.to_string())
    );
    assert_eq!(
        w(fwd("default", None, Some("https://p.example"), "packaged")),
        (BUILT_IN.to_string(), String::new())
    );
    assert_eq!(w(fwd("off", None, None, "setting")).0, "Off");
    assert_eq!(
        w(fwd(
            "url",
            Some("http://127.0.0.1:8095"),
            Some("http://127.0.0.1:8095"),
            "setting"
        ))
        .0,
        "http://127.0.0.1:8095"
    );
    let flag = forwarder(&fwd("off", None, Some("https://f.example"), "flag")).unwrap();
    assert_eq!(
        flag.words(),
        ("https://f.example".to_string(), FLAG_WINS.to_string())
    );
    assert_eq!(
        flag.later().unwrap(),
        "Your saved choice (Off) applies when the gateway starts without the flag."
    );
    assert!(forwarder(&json!({"forwarder": {"mode": "sometimes", "source": "flag"}})).is_none());
    assert_eq!(put_body("off", "x"), json!({"mode": "off"}));
    assert_eq!(
        put_body("url", " http://127.0.0.1:8095 "),
        json!({"mode": "url", "url": "http://127.0.0.1:8095"})
    );
    assert_eq!(probe_words(&json!({})), "Saved.");
    assert_eq!(
        probe_words(&json!({"probe": {"reachable": true, "topics": ["a.b"], "reason": null}})),
        "Saved. The forwarder answered: it pushes to a.b."
    );
    assert_eq!(
        probe_words(
            &json!({"probe": {"reachable": false, "topics": [], "reason": "Could not reach it."}})
        ),
        "Saved, but the forwarder could not be reached: Could not reach it."
    );
}

#[test]
fn a_member_sees_no_card_and_sends_no_read() {
    let mut h = page(false);
    let reads = h
        .sent()
        .into_iter()
        .filter(|c| matches!(c, Cmd::Json(JsonCmd::Get { key, .. }) if key == K_FORWARDER))
        .count();
    assert_eq!(reads, 0);
    let s = scroll_to(&mut h, "Agent email tools");
    assert!(!s.contains("Push forwarder"), "{s}");
}

#[test]
fn an_admin_reads_edits_and_saves() {
    let mut h = page(true);
    let reads: Vec<String> = h
        .sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) if key == K_FORWARDER => Some(path),
            _ => None,
        })
        .collect();
    assert_eq!(reads, vec!["/push/settings".to_string()]);
    h.store.json.set(
        K_FORWARDER,
        Loadable::Ready(fwd("default", None, None, "packaged")),
    );
    let s = scroll_to(&mut h, "Built-in forwarder ·");
    assert!(s.contains(&FORWARDER_HELP[..40]), "{s}");
    assert!(s.contains(NONE_BUILT_IN), "{s}");
    h.shoot("my-email-tui-push-forwarder");

    // The flag wins: the address it set, and when the saved choice applies.
    h.store.json.set(
        K_FORWARDER,
        Loadable::Ready(fwd(
            "url",
            Some("http://127.0.0.1:8095"),
            Some("https://f.example"),
            "flag",
        )),
    );
    let s = scroll_to(&mut h, FLAG_WINS);
    assert!(s.contains("https://f.example"), "{s}");
    assert!(
        s.contains("Your saved choice (http://127.0.0.1:8095) applies"),
        "{s}"
    );

    // Edit: the three choices; the saved one picked; the address field for "Your own forwarder".
    h.click_text("Edit");
    let s = scroll_to(&mut h, "Your own forwarder");
    for w in [
        "Send alerts through",
        "Built-in forwarder",
        "Off",
        "Its address",
        "http://127.0.0.1:8095",
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    h.shoot("my-email-tui-push-forwarder-edit");
    h.sent();
    h.click_text("Save");
    let puts: Vec<(String, Value)> = h
        .sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                key,
                method,
                path,
                body,
                ..
            }) if key == K_FORWARDER_WRITE => {
                assert_eq!(method, "PUT");
                Some((path, body))
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        puts,
        vec![(
            "/push/settings".to_string(),
            json!({"mode": "url", "url": "http://127.0.0.1:8095"})
        )]
    );
    // Refused: the gateway's sentence, the form stays open.
    h.store.json.set_write(
        K_FORWARDER_WRITE,
        Some(WriteState::Failed(ApiError {
            kind: ApiErrorKind::Http(422),
            message: "Use https:// for a forwarder on another machine.".into(),
            body: Some(json!({"detail": "Use https:// for a forwarder on another machine."})),
            timed_out: false,
        })),
    );
    let s = scroll_to(&mut h, "Use https:// for a forwarder on another machine.");
    assert!(s.contains("Its address"), "{s}");
    // Saved: the probe's answer, the row again.
    h.click_text("Save");
    h.store.json.set_write(
        K_FORWARDER_WRITE,
        Some(WriteState::Done(json!({
            "forwarder": {"mode": "url", "url": "http://127.0.0.1:8095", "effective": "http://127.0.0.1:8095", "source": "setting", "packaged": null},
            "probe": {"reachable": true, "topics": ["ai.abstractframework.assistant"], "reason": null}
        }))),
    );
    h.store.json.set(
        K_FORWARDER,
        Loadable::Ready(fwd(
            "url",
            Some("http://127.0.0.1:8095"),
            Some("http://127.0.0.1:8095"),
            "setting",
        )),
    );
    let s = scroll_to(
        &mut h,
        "Saved. The forwarder answered: it pushes to ai.abstractframework.assistant.",
    );
    assert!(!s.contains("Its address"), "{s}");
    assert!(s.contains("http://127.0.0.1:8095"), "{s}");
}
