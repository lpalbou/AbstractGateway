//! R14.3 (round 14): the header's memory/compute widget (R10.3: the web
//! top bar's line replaces the address once signed in), the "Last restart"
//! row of the Gateway card (F3; R13.1 via R14-W1's `/host/runner`
//! `last_hang`), and the Apps rows' Assistant sentences (R10.5/R10.6)
//! against the recorded `/apps` answer. Hermetic; captures land in
//! R8W4_SHOTS_DIR.

mod r8w4;

use abstractgateway_console::store::apps::AppsOverview;
use abstractgateway_console::store::operator::HostRunner;
use abstractgateway_console::store::{host_state_from_payload, Loadable};
use abstractgateway_console::ui::{self, apps};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};
use serde_json::{json, Value};

fn fx(name: &str) -> Value {
    let path = format!(
        "{}/tests/fixtures/r14w3_{name}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
}

/// The F3 panel's text alone (the page around the modal folded away):
/// each line's cells between the panel's left border and its right edge.
fn panel_text(s: &str) -> String {
    let title = s
        .lines()
        .find(|l| l.contains("Gateway host"))
        .expect("the F3 panel");
    let chars: Vec<char> = title.chars().collect();
    let at = title.find("Gateway host").unwrap();
    let left = title[..at].chars().count() - 1;
    assert_eq!(chars[left], '│');
    let mut out = Vec::new();
    for l in s.lines() {
        let cs: Vec<char> = l.chars().collect();
        if cs.len() <= left || cs[left] != '│' {
            continue;
        }
        let rest: String = cs[left + 1..]
            .iter()
            .take_while(|c| **c != '│' && **c != '┃')
            .collect();
        out.push(rest);
    }
    out.join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn host() -> Value {
    json!({
        "memory": {"ram": {"total_bytes": 68719476736u64, "used_bytes": 19541180416u64, "percent": 28.4}},
        "gpu": {"supported": true, "utilization_gpu_pct": 3.2, "source": "ioreg"},
        "totals": {"models_resident": 1, "model_bytes": 4294967296u64}
    })
}

#[test]
fn signed_in_the_header_shows_the_resources_widget_instead_of_the_address() {
    for size in SIZES {
        let mut h = harness(size, Mount::Root);
        let s = h.turns(3);
        let top = s.lines().next().unwrap().to_string();
        assert!(
            top.contains("http://127.0.0.1:18999"),
            "not signed in: the address\n{s}"
        );
        h.admin();
        h.turns(2);
        // The widget asks for the snapshot at once (then every 5 s).
        let sent = h.sent();
        assert!(
            sent.iter().any(|c| matches!(c, Cmd::RefreshHostWidget)),
            "{sent:?}"
        );
        // Nothing read yet: dashes, never zeros.
        let s = h.turns(2);
        assert!(s.lines().next().unwrap().contains("Mem — · GPU —"), "{s}");
        h.store
            .host_state
            .set(Loadable::Ready(host_state_from_payload(&host())));
        let s = h.shoot("r14w3-header-widget");
        let top = s.lines().next().unwrap().to_string();
        assert!(!top.contains("http://127.0.0.1:18999"), "{top}");
        if size.0 >= 120 {
            assert!(
                top.contains("Mem 18.2 GiB / 64.0 GiB (28%) · GPU 3% · 1 model"),
                "{top}"
            );
        } else {
            assert!(top.contains("Mem 28% · GPU 3% · 1 model"), "{top}");
        }
        h.assert_fits();
    }
}

#[test]
fn the_widget_poll_pauses_on_the_resources_page() {
    let mut h = harness((120, 40), Mount::Root);
    h.admin();
    h.turns(2);
    h.sent();
    h.key(b"H");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_MODELS);
    h.sent();
    std::thread::sleep(std::time::Duration::from_millis(5300));
    h.turns(3);
    let sent = h.sent();
    assert!(
        !sent.iter().any(|c| matches!(c, Cmd::RefreshHostWidget)),
        "the Resources page's own chain refreshes the snapshot: {sent:?}"
    );
    h.key(b"2");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::RefreshHostWidget)),
        "{sent:?}"
    );
}

#[test]
fn f3_shows_the_last_watchdog_restart_word_for_word() {
    for size in SIZES {
        let mut h = harness(size, Mount::Root);
        h.admin();
        h.store.op.runner.set(Loadable::Ready(HostRunner::from_value(&json!({
            "paused": false, "inflight_ticks": 0,
            "capabilities": {"restart": true, "shutdown": true},
            "last_hang": {
                "at": "2026-10-04T19:23:57+00:00", "stamp": "20261004T192357Z", "blocked_s": 31.5,
                "reason": "the event loop was blocked in starlette/responses.py:245 listen_for_disconnect (called from abstractgateway/security/gateway_security.py:1443) while serving POST /api/gateway/runs/c45d/voice/tts/stream",
                "top_frame": "starlette/responses.py:245 listen_for_disconnect",
                "dump_path": "/data/incidents/watchdog-20261004T192357Z.threads.txt",
                "file": "/data/incidents/watchdog-20261004T192357Z.json",
                "line": "Gateway restarted at 2026-10-04T19:23:57+00:00 after a hang — …"
            }
        }))));
        h.turns(2);
        h.key(b"\x1bOR"); // F3
        let s = h.shoot("r14w3-f3-last-restart");
        let flat = panel_text(&s);
        assert!(
            flat.contains("last restart: Gateway restarted at 2026-10-04 19:23:57 +00:00 after a hang — the event loop was blocked in starlette/responses.py:245 listen_for_disconnect"),
            "{s}"
        );
        assert!(
            flat.contains(
                "Every thread's stack: /data/incidents/watchdog-20261004T192357Z.threads.txt"
            ),
            "{s}"
        );
        h.assert_fits();
        // The rows scroll when the terminal is short (80x24): never clipped.
        let s = h.wheel_down(6);
        let flat = panel_text(&s);
        assert!(
            flat.contains("Blocked 31.5 s in starlette/responses.py:245 listen_for_disconnect"),
            "{s}"
        );
        assert!(
            flat.contains("Incident file: /data/incidents/watchdog-20261004T192357Z.json"),
            "{s}"
        );
    }
    // No incident: no row.
    let mut h = harness((120, 40), Mount::Root);
    h.admin();
    h.store.op.runner.set(Loadable::Ready(HostRunner::from_value(
        &json!({"paused": false, "inflight_ticks": 0, "capabilities": {"restart": true, "shutdown": true}, "last_hang": null}),
    )));
    h.turns(2);
    h.key(b"\x1bOR");
    assert!(!h.turns(2).contains("last restart"));
}

fn apps_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    apps::view(cx, ctx, &t)
}

fn apps_page(overview: &Value) -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(apps_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(overview)));
    let i = h
        .store
        .apps
        .overview
        .with_untracked(|o| {
            o.ready()
                .unwrap()
                .apps
                .iter()
                .position(|a| a.id == "assistant")
        })
        .unwrap();
    h.store.apps.sel.set(i);
    h.turns(3);
    h
}

/// The recorded `/apps` answer (a source-checkout Assistant on a scratch
/// gateway): its version sentence shows ONLY for a source checkout (the
/// web card's rule), the badge is the stop/start control.
#[test]
fn the_assistant_rows_sentences_follow_the_web_card() {
    let mut v = fx("apps");
    let a = v["apps"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|a| a["id"] == "assistant")
        .unwrap();
    assert_eq!(
        a["desktop"]["source_checkout"], true,
        "recorded from a checkout"
    );
    a["desktop"]["version_reason"] =
        json!("Installed from a source checkout: its version is the checkout's.");
    let mut h = apps_page(&v);
    let s = h.shoot("r14w3-apps-assistant");
    assert!(
        s.contains("Installed from a source checkout: its version is the checkout's."),
        "{s}"
    );
    assert!(s.contains("Stopped — click to start"), "{s}");
    // Not a source checkout: the sentence is not the card's.
    let a = v["apps"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|a| a["id"] == "assistant")
        .unwrap();
    a["desktop"]["source_checkout"] = json!(false);
    let mut h = apps_page(&v);
    let s = h.turns(2);
    assert!(
        !s.contains("Installed from a source checkout"),
        "version_reason without a source checkout:\n{s}"
    );
}
