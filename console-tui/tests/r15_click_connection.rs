//! R15 (DESIGN-TUI.md §3.3): a synthesized mouse click for EVERY Connection
//! button — Sign in / Re-probe, Show / Hide on the token, Network, and the
//! sign-in-by-email steps (the link, Use code, Send a new code, Back to
//! token, Copy / Done on the new token) — through the real input pipeline.
//! The meta-test enumerates `connection::page_actions` and
//! `connection::recovery_actions` over every state: an action without a
//! click test here is RED. The words are the web sign-in card's
//! (`tests/fixtures/r15_web_wording_connection.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::email::{RecoveryAnswer, RecoveryStep};
use abstractgateway_console::store::ConnPhase;
use abstractgateway_console::ui::{self, connection};
use abstractgateway_console::worker::operator::{OpCmd, RecoveryAction};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::Value;

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    connection::view(cx, ctx, &t)
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_connection.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn signed_out() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.ui.wizard.set(false);
    h.store.conn.set(ConnPhase::NotConnected);
    h.ui.conn_url.set("http://127.0.0.1:18999".into());
    h.turns(3);
    h.sent();
    h
}

fn signed_in() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    h.turns(3);
    h.sent();
    h
}

/// Signed out, the gateway offers sign-in by email, at `step`.
fn recovery(step: RecoveryStep) -> r8w4::Harness {
    let mut h = signed_out();
    h.store.op.recovery.update(|r| {
        r.checked_url = "http://127.0.0.1:18999".into();
        r.available = Some(true);
        r.step = step;
    });
    h.turns(3);
    h.sent();
    h
}

fn answer(sent: bool, at_ms: u64) -> RecoveryAnswer {
    RecoveryAnswer {
        user_id: "admin".into(),
        sent,
        to: "a***@example.test".into(),
        message: "We sent a code to a***@example.test.".into(),
        reason_code: String::new(),
        retry_after_s: None,
        at_ms,
    }
}

/// Click the button whose face is exactly `label` (the LAST line showing
/// " label ": a sentence above may hold the same word).
fn click_button(h: &mut r8w4::Harness, label: &str) -> String {
    let s = h.turns(1);
    let needle = format!(" {label} ");
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&needle))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] button:\n{s}"));
    let b = line.find(&needle).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn recovery_cmds(cmds: Vec<Cmd>) -> Vec<RecoveryAction> {
    cmds.into_iter()
        .filter_map(|c| match c {
            Cmd::Operator(OpCmd::Recovery(a)) => Some(a),
            _ => None,
        })
        .collect()
}

fn offered() -> BTreeSet<&'static str> {
    let mut out = BTreeSet::new();
    for connected in [false, true] {
        for revealed in [false, true] {
            for a in connection::page_actions(connected, revealed) {
                out.insert(a.id);
            }
        }
    }
    for (step, ready, wait) in [
        ("idle", false, 0),
        ("code", true, 0),
        ("code", false, 5),
        ("token", false, 0),
    ] {
        for a in connection::recovery_actions(step, ready, wait, true) {
            out.insert(a.id);
        }
    }
    out
}

fn covered() -> BTreeSet<&'static str> {
    [
        "probe", "reveal", "network", "recover", "use_code", "resend", "back", "copy", "done",
    ]
    .into_iter()
    .collect()
}

#[test]
fn every_offered_connection_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Connection actions without a click test: {missing:?}"
    );
}

#[test]
fn sign_in_and_re_probe_are_buttons() {
    let mut h = signed_out();
    click_button(&mut h, "Sign in");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::Connect { .. })),
        "Sign in probes the gateway"
    );
    let mut h = signed_in();
    let s = h.turns(1);
    assert!(
        !s.contains(" Sign in "),
        "connected: Re-probe, not Sign in\n{s}"
    );
    click_button(&mut h, "Re-probe");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Connect { .. })));
}

#[test]
fn show_and_hide_reveal_the_token() {
    let mut h = signed_out();
    h.ui.conn_token.set("tok-secret-123".into());
    let s = h.turns(3);
    assert!(!s.contains("tok-secret-123"), "masked at first:\n{s}");
    let s = click_button(&mut h, "Show");
    assert!(s.contains("tok-secret-123"), "Show reveals it:\n{s}");
    let s = click_button(&mut h, "Hide");
    assert!(!s.contains("tok-secret-123"), "Hide masks it again:\n{s}");
    assert!(s.contains(" Show "), "{s}");
}

#[test]
fn the_network_button_opens_network() {
    let mut h = signed_in();
    click_button(&mut h, "Network");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_NETWORK);
    // Signed out there is no Network button (nothing to read yet).
    let mut h = signed_out();
    let s = h.turns(1);
    assert!(!s.contains(" Network "), "{s}");
}

#[test]
fn the_recovery_link_asks_for_a_code() {
    let mut h = recovery(RecoveryStep::Idle);
    h.click_text(connection::RECOVERY_LINK);
    let sent = recovery_cmds(h.sent());
    assert!(
        matches!(sent.as_slice(), [RecoveryAction::Request { user_id, .. }] if user_id == "admin"),
        "{sent:?}"
    );
}

#[test]
fn the_code_step_buttons_do_what_they_say() {
    // Use code: refused (faint, says why) until the 8 digits are typed.
    let mut h = recovery(RecoveryStep::Code(answer(true, 0)));
    click_button(&mut h, "Use code");
    assert!(
        recovery_cmds(h.sent()).is_empty(),
        "no redeem before 8 digits"
    );
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Type the 8 digits from the email first.")
    );
    h.click_text("8 digits");
    h.type_text("12345678");
    click_button(&mut h, "Use code");
    let sent = recovery_cmds(h.sent());
    assert!(
        matches!(sent.as_slice(), [RecoveryAction::Redeem { user_id, .. }] if user_id == "admin"),
        "{sent:?}"
    );
    // Send a new code (the cooldown is over: the answer is old).
    let mut h = recovery(RecoveryStep::Code(answer(true, 0)));
    h.click_text("Send a new code");
    assert!(matches!(
        recovery_cmds(h.sent()).as_slice(),
        [RecoveryAction::Request { .. }]
    ));
    // Back to token.
    let mut h = recovery(RecoveryStep::Code(answer(true, 0)));
    h.click_text("Back to token");
    assert_eq!(
        h.store.op.recovery.with_untracked(|r| r.step.clone()),
        RecoveryStep::Idle
    );
}

#[test]
fn the_new_token_has_copy_and_done() {
    let mut h = signed_out();
    h.store.op.recovery.update(|r| {
        r.new_token = Some("tok-new-456".into());
        r.signed_in_user = "admin".into();
    });
    let s = h.turns(3);
    assert!(s.contains("tok-new-456"), "{s}");
    click_button(&mut h, "Copy");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("token copied to the clipboard")
    );
    let s = click_button(&mut h, "Done");
    assert!(
        h.store
            .op
            .recovery
            .with_untracked(|r| r.new_token.is_none()),
        "{s}"
    );
}

#[test]
fn hovering_show_says_the_web_tooltip() {
    let mut h = signed_out();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find(" Show ").map(|c| (i, l[..c].chars().count() + 1)))
        .expect("Show button");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Show token"), "{s}");
}

#[test]
fn the_keyboard_reaches_sign_in() {
    // Enter in the token field signs in (the URL field has the caret
    // first; Tab moves to the token).
    let mut h = signed_out();
    h.key(b"\t");
    h.type_text("tok");
    h.key(b"\r");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Connect { .. })));
}

#[test]
fn the_words_are_the_web_sign_in_cards() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(connection::TOKEN_LABEL, w("token_label"));
    assert_eq!(connection::SIGN_IN, w("sign_in"));
    assert_eq!(connection::RECOVERY_LINK, w("recovery_link"));
    let by_id = |acts: Vec<abstractgateway_console::ui::w::Action>, id: &str| {
        acts.into_iter().find(|a| a.id == id).expect(id)
    };
    let show = by_id(connection::page_actions(false, false), "reveal");
    assert_eq!(
        (show.label.clone(), show.tooltip.clone()),
        (w("show"), Some(w("show_tip")))
    );
    let hide = by_id(connection::page_actions(false, true), "reveal");
    assert_eq!(
        (hide.label.clone(), hide.tooltip.clone()),
        (w("hide"), Some(w("hide_tip")))
    );
    assert_eq!(
        by_id(connection::page_actions(false, false), "probe").label,
        w("sign_in")
    );
    assert_eq!(
        by_id(connection::page_actions(true, false), "network").tooltip,
        Some(w("network_tip"))
    );
    let code = connection::recovery_actions("code", true, 0, true);
    assert_eq!(by_id(code.clone(), "use_code").label, w("use_code"));
    assert_eq!(by_id(code.clone(), "resend").label, w("resend"));
    assert_eq!(by_id(code, "back").label, w("back"));
    assert_eq!(
        by_id(
            connection::recovery_actions("idle", false, 0, true),
            "recover"
        )
        .label,
        w("recovery_link")
    );
    // The code field's label on screen.
    let h_screen = {
        let mut h = recovery(RecoveryStep::Code(answer(true, 0)));
        h.turns(2)
    };
    assert!(h_screen.contains(&w("code_label")), "{h_screen}");
}
