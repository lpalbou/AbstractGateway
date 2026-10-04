//! Round 10 (Y1): the Workspaces screen is PARKED. The gateway moved
//! workspaces into Accounts in round 9 and removed the routes the old
//! screen called (`/admin/user-workspace-policy`, `/workspace/policy/self`,
//! the runtime-config `workspace_*` keys). The screen shows exactly one
//! sentence and sends nothing; the sidebar entry stays. Hermetic: the
//! harness's command channel is the only way out of the UI, so "no command
//! sent" = "no request made". Live drive (ignored by default) against a
//! scratch gateway, whose access log then shows no workspace route:
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r10w5_workspaces -- --ignored --test-threads 1

mod r8w4;

use abstractgateway_console::ui::{self, workspaces};
use r8w4::{harness, live, live_env, Mount, SIZES};

const SENTENCE: &str = "Workspaces are managed from Accounts in the web console; the terminal console follows in the next update.";

/// The screen's text with borders and line breaks folded away.
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' ' || c == '┃'))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    workspaces::view(cx, ctx, &t)
}

#[test]
fn the_parked_sentence_is_the_page_constant() {
    assert_eq!(workspaces::PARKED, SENTENCE);
}

/// `W` from the whole console, as an admin and as a user: the one
/// sentence, none of the old controls, and not one command sent.
#[test]
fn w_shows_one_sentence_and_sends_nothing() {
    for admin in [true, false] {
        for size in SIZES {
            let mut h = harness(size, Mount::Root);
            h.identity(if admin { "admin" } else { "bob" }, admin);
            h.turns(3);
            h.sent(); // whatever the shell read on sign-in
            h.key(b"W");
            let s = h.shoot(if admin {
                "workspaces-admin"
            } else {
                "workspaces-user"
            });
            assert_eq!(
                h.ui.screen.get_untracked(),
                ui::SCREEN_WORKSPACES,
                "W opens Workspaces:\n{s}"
            );
            assert!(flat(&s).contains(SENTENCE), "admin={admin} {size:?}:\n{s}");
            for gone in [
                "Any folder",
                "old clients",
                "Own policy",
                "Launch-folder",
                "Allowed folders",
                "Refused folders",
                "Default folder",
                "Gateway policy",
                "Loading",
                "Could not read",
            ] {
                assert!(!s.contains(gone), "{gone:?} still shown:\n{s}");
            }
            // Stay on the page a while (effects, ticks): still nothing.
            h.turns(10);
            let sent = h.sent();
            assert!(
                sent.is_empty(),
                "admin={admin}: the parked page sent {sent:?}"
            );
            h.assert_fits();
        }
    }
}

/// The page alone: Enter, r, x, arrows — no verb, no command, no overlay.
#[test]
fn no_key_on_the_page_does_anything() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.admin();
    h.turns(3);
    h.sent();
    let before = h.text();
    for k in [&b"\r"[..], b"r", b"x", b" ", b"e", b"\x1b[B", b"\x1b[A"] {
        h.key(k);
    }
    let after = h.text();
    assert_eq!(before, after, "the page changed on a key");
    assert!(flat(&after).contains(SENTENCE), "{after}");
    let sent = h.sent();
    assert!(sent.is_empty(), "{sent:?}");
}

/// The footer offers no verb on this page.
#[test]
fn the_page_has_no_footer_verb() {
    let mut h = harness((120, 40), Mount::Root);
    h.admin();
    h.key(b"W");
    let s = h.text();
    let footer = s.lines().last().unwrap_or_default().to_string();
    for verb in ["edit policy", "refresh"] {
        assert!(!footer.contains(verb), "{verb:?} in the footer: {footer}");
    }
}

// ------------------------------------------------------------------ live

/// Live: the real worker against a scratch gateway — the sentence renders
/// and the page reads nothing (the recipe greps the gateway's access log
/// for the removed routes afterwards).
#[test]
#[ignore]
fn live_w_shows_the_sentence() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let mut h = live((120, 40), Mount::Root, &url, &token);
    h.until("signed in", |h, _| {
        h.store
            .conn
            .with_untracked(abstractgateway_console::store::ConnPhase::is_connected)
    });
    h.ui.wizard.set(false);
    h.key(b"W");
    let s = h.until("the parked sentence", |_, s| flat(s).contains(SENTENCE));
    h.turns(40);
    let _ = h.shoot("live-workspaces");
    assert!(!s.contains("Any folder"), "{s}");
}
