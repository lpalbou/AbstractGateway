//! R15: the F3 host panel is the web's ◎ Gateway card (Resources) — a
//! synthesized mouse click for EVERY control: the Workflows paused and
//! Start at login switches, Check now, Update, Restart gateway…, Quit
//! gateway… and Close — through the real input pipeline (the whole
//! console mounted, F3 opens it). The meta-test enumerates
//! `host::host_actions` (+ the two switches): an action without a click
//! test is RED. The words are the web card's
//! (`tests/fixtures/r15_web_wording_host.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::operator::{HostRunner, HostUpdate, StartAtLogin};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, host};
use abstractgateway_console::worker::operator::OpCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn runner(paused: bool) -> HostRunner {
    HostRunner::from_value(&json!({
        "paused": paused, "paused_at": "2026-10-08T07:00:00+00:00", "paused_by": "default/admin",
        "inflight_ticks": 0, "runner_in_process": true,
        "capabilities": {"restart": true, "shutdown": true, "reason": null}
    }))
}

fn update() -> HostUpdate {
    HostUpdate::from_value(&json!({
        "current": "0.13.1",
        "install": {"kind": "pip", "upgradable": true},
        "check": {"update_available": true, "latest": "0.13.2", "checked_at": "2026-10-08T07:00:00Z"},
        "update": {"status": "available", "line": "0.13.1 — 0.13.2 is available", "hint": "installed with pip",
                   "action": {"label": "Update", "confirm": "Update AbstractGateway to 0.13.2? It installs in the background; restart to finish."}}
    }))
}

fn login(enabled: bool) -> StartAtLogin {
    StartAtLogin::from_value(&json!({
        "schema": "gateway_start_at_login_v1", "enabled": enabled, "state": if enabled { "on" } else { "off" },
        "mechanism": "systemd-user", "mechanism_label": "a systemd user unit", "can_change": true, "reason": null,
        "summary": if enabled { "On — a systemd user unit starts the gateway at login" } else { "Off — nothing starts the gateway at login" }
    }))
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_host.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

/// The console with the F3 panel open over it (admin).
fn panel_sized(size: (i32, i32), admin: bool) -> r8w4::Harness {
    let mut h = harness(size, Mount::Root);
    h.identity(if admin { "admin" } else { "bob" }, admin);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.store.op.runner.set(Loadable::Ready(runner(false)));
    h.store.op.update.set(Loadable::Ready(update()));
    h.store.op.start_at_login.set(Loadable::Ready(login(false)));
    h.store.op.tray.set(Loadable::Ready(
        "On the menu bar while the gateway runs".into(),
    ));
    h.turns(2);
    h.key(b"\x1bOR");
    // Opening re-reads: put the answers back (the worker is not under test).
    h.store.op.runner.set(Loadable::Ready(runner(false)));
    h.store.op.update.set(Loadable::Ready(update()));
    h.store.op.start_at_login.set(Loadable::Ready(login(false)));
    h.turns(3);
    h.sent();
    h
}

fn panel() -> r8w4::Harness {
    panel_sized((130, 44), true)
}

/// Click the last on-screen occurrence of `label` (above the status row,
/// which names the focused control).
fn click_last(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let n = screen.lines().count();
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(i, l)| *i + 1 < n && l.contains(label))
        .last()
        .unwrap_or_else(|| panic!("no {label:?}:\n{screen}"));
    let x = line[..line.rfind(label).unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn ops(h: &mut r8w4::Harness) -> Vec<OpCmd> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Operator(o) => Some(o),
            _ => None,
        })
        .collect()
}

fn offered() -> BTreeSet<&'static str> {
    let mut out: BTreeSet<&'static str> =
        host::host_actions(Some(&runner(false)), Some(&update()), true)
            .into_iter()
            .map(|a| a.id)
            .collect();
    out.insert("pause");
    out.insert("login");
    out.insert("close");
    out
}

fn covered() -> BTreeSet<&'static str> {
    [
        "check", "update", "restart", "quit", "pause", "login", "close",
    ]
    .into_iter()
    .collect()
}

#[test]
fn every_offered_host_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "host panel actions without a click test: {missing:?}"
    );
}

#[test]
fn the_panel_is_the_web_gateway_card() {
    let mut h = panel();
    let s = h.turns(1);
    for needle in [
        "Gateway",
        "How this gateway is running right now.",
        "Workflows",
        "Version",
        "0.13.1 — 0.13.2 is available",
        "Desktop icon",
        "On the menu bar while the gateway runs",
        "Start at login",
        "●─ Workflows paused",
        "●─ Start at login",
        "Check now",
        "Update",
        "Restart gateway…",
        "Quit gateway…",
        "Close",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    h.assert_fits();
    let mut h = panel_sized((80, 24), true);
    let s = h.turns(1);
    assert!(s.contains("Quit gateway…") && s.contains("Close"), "{s}");
    h.assert_fits();
}

#[test]
fn the_switches_by_mouse() {
    let mut h = panel();
    click_last(&mut h, "Workflows paused");
    assert!(ops(&mut h)
        .iter()
        .any(|o| matches!(o, OpCmd::SetPaused { pause: true })));
    let mut h = panel();
    click_last(&mut h, "Start at login");
    let s = h.turns(2);
    assert!(s.contains("Start AbstractGateway at login?"), "{s}");
    // The confirm's action is the gateway's verb (StartAtLogin::verb).
    click_last(&mut h, "Start at login");
    let o = ops(&mut h);
    assert!(
        o.iter()
            .any(|o| matches!(o, OpCmd::SetStartAtLogin { enabled: true, .. })),
        "{o:?}\n{s}"
    );
}

#[test]
fn the_buttons_by_mouse() {
    let mut h = panel();
    click_last(&mut h, "Check now");
    assert!(ops(&mut h).iter().any(|o| matches!(o, OpCmd::UpdateCheck)));
    // Update: the gateway's sentence, Update now / Cancel.
    let mut h = panel();
    click_last(&mut h, "Update");
    let s = h.turns(2);
    assert!(s.contains("Update AbstractGateway to 0.13.2?"), "{s}");
    let s2 = click_last(&mut h, "Update now");
    let s3 = h.turns(2);
    assert!(
        ops(&mut h)
            .iter()
            .any(|o| matches!(o, OpCmd::UpdateStart { .. })),
        "{s}\n{s2}\n{s3}"
    );
    // Restart: the web's question, Restart / Cancel.
    let mut h = panel();
    click_last(&mut h, "Restart gateway…");
    let s = h.turns(2);
    assert!(
        s.contains("Restart AbstractGateway? Running workflows pause"),
        "{s}"
    );
    click_last(&mut h, "Restart");
    assert!(ops(&mut h).iter().any(|o| matches!(o, OpCmd::Restart)));
    // Quit: Quit / Cancel; Cancel keeps it running.
    let mut h = panel();
    click_last(&mut h, "Quit gateway…");
    let s = h.turns(2);
    assert!(s.contains("Quit AbstractGateway?"), "{s}");
    click_last(&mut h, "Cancel");
    assert!(!ops(&mut h).iter().any(|o| matches!(o, OpCmd::Shutdown)));
    let mut h = panel();
    click_last(&mut h, "Quit gateway…");
    h.turns(2);
    click_last(&mut h, "Quit");
    assert!(ops(&mut h).iter().any(|o| matches!(o, OpCmd::Shutdown)));
    // Close.
    let mut h = panel();
    click_last(&mut h, "Close");
    let s = h.turns(2);
    assert!(!s.contains("How this gateway is running right now."), "{s}");
}

#[test]
fn a_launch_that_cannot_restart_refuses_with_its_reason() {
    let mut h = panel();
    let mut r = runner(false);
    r.cap_restart = false;
    r.cap_reason = "started with --reload".into();
    h.store.op.runner.set(Loadable::Ready(r));
    h.turns(2);
    click_last(&mut h, "Restart gateway…");
    h.turns(2);
    assert!(!ops(&mut h).iter().any(|o| matches!(o, OpCmd::Restart)));
    assert!(
        h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .contains("started with --reload"),
        "{:?}",
        h.store.notice.get_untracked()
    );
}

#[test]
fn hovering_a_control_shows_the_web_tooltip() {
    let mut h = panel();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .filter_map(|(i, l)| l.rfind("Check now").map(|c| (i, l[..c].chars().count())))
        .last()
        .expect("Check now");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Check for a newer release"), "{s}");
}

#[test]
fn the_keys_reach_every_verb() {
    let mut h = panel();
    h.key(b"p");
    assert!(ops(&mut h)
        .iter()
        .any(|o| matches!(o, OpCmd::SetPaused { .. })));
    let mut h = panel();
    h.key(b"u");
    assert!(ops(&mut h).iter().any(|o| matches!(o, OpCmd::UpdateCheck)));
    let mut h = panel();
    let s = h.key(b"R");
    let s = if s.contains("Restart AbstractGateway?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Restart AbstractGateway?"), "{s}");
    let mut h = panel();
    let s = h.key(b"Q");
    let s = if s.contains("Quit AbstractGateway?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Quit AbstractGateway?"), "{s}");
    let mut h = panel();
    let s = h.key(b"U");
    let s = if s.contains("Update AbstractGateway to") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Update AbstractGateway to"), "{s}");
    let mut h = panel();
    let s = h.key(b"L");
    let s = if s.contains("Start AbstractGateway at login?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Start AbstractGateway at login?"), "{s}");
    let mut h = panel();
    h.key(b"r");
    assert!(ops(&mut h)
        .iter()
        .any(|o| matches!(o, OpCmd::LoadHost { .. })));
}

#[test]
fn a_non_admin_sees_the_card_and_the_reason() {
    let mut h = panel_sized((130, 44), false);
    let s = h.turns(1);
    assert!(
        s.contains("only an admin can pause, restart, quit or update this gateway"),
        "{s}"
    );
    assert!(
        !s.contains("Quit gateway…") && !s.contains("Workflows paused"),
        "{s}"
    );
    h.key(b"p");
    assert!(!ops(&mut h)
        .iter()
        .any(|o| matches!(o, OpCmd::SetPaused { .. })));
}

#[test]
fn the_panel_survives_the_reads_it_causes() {
    let mut h = panel();
    h.store.op.runner.set(Loadable::Ready(runner(true)));
    h.store.op.update.set(Loadable::Ready(update()));
    let s = h.turns(3);
    assert!(
        s.contains("How this gateway is running right now.") && s.contains("━● Workflows paused"),
        "{s}"
    );
}

#[test]
fn the_words_are_the_web_cards() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(host::TITLE, w("title"));
    assert_eq!(host::NOTE, w("note"));
    assert_eq!(host::PAUSE_LABEL, w("pause"));
    assert_eq!(host::PAUSE_TIP, w("pause_tip"));
    assert_eq!(host::CHECK_TIP, w("check_tip"));
    assert_eq!(host::UPDATE_TIP, w("update_tip"));
    assert_eq!(host::RESTART_QUESTION, w("restart_question"));
    assert_eq!(host::QUIT_QUESTION, w("quit_question"));
    let labels: Vec<String> = host::host_actions(Some(&runner(false)), Some(&update()), true)
        .into_iter()
        .map(|a| a.label)
        .collect();
    assert_eq!(
        labels,
        vec![w("check"), w("update"), w("restart"), w("quit")]
    );
    let mut h = panel();
    let s = h.turns(1);
    for k in ["row_0", "row_1", "row_3", "login"] {
        assert!(s.contains(&w(k)), "{k}:\n{s}");
    }
    click_last(&mut h, "Restart gateway…");
    let s = h.turns(2);
    assert!(
        s.contains(&format!(" {} ", w("restart_go"))) && s.contains("Cancel"),
        "{s}"
    );
}
