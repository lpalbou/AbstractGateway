//! R14.3 ← R14.2: Accounts row "Preferences" (`p`) = the web modal word for
//! word (R14 PREFERENCES API — FINAL): the default workflow per app, the
//! gateway default first, applies on change (one PUT of that interface),
//! "Saved." / "Not saved. <message>". Hermetic over the recorded answers
//! of R14-W2's route (tests/fixtures/r14w3_prefs_*.json, recorded from a
//! scratch gateway on W2's branch). Captures land in R8W4_SHOTS_DIR.

mod r8w4;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, users};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};
use serde_json::{json, Value};

const UP: &[u8] = b"\x1b[A";
const DOWN: &[u8] = b"\x1b[B";

fn fx(name: &str) -> Value {
    let path = format!(
        "{}/tests/fixtures/r14w3_{name}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
}

fn users_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

/// R15: a form modal is a centred box over the page — read only what is
/// inside the outermost box when one is open (the page shows beside it).
fn inside_modal(s: &str) -> String {
    let lines: Vec<Vec<char>> = s.lines().map(|l| l.chars().collect()).collect();
    let mut best: Option<(usize, usize, usize)> = None; // (row, x0, x1)
    for (y, l) in lines.iter().enumerate() {
        if let (Some(x0), Some(x1)) = (
            l.iter().position(|c| *c == '╭'),
            l.iter().rposition(|c| *c == '╮'),
        ) {
            // A modal box starts its top border with ╭ then a run of ─.
            let w = x1.saturating_sub(x0);
            if w > 20
                && best.map(|(_, a, b)| w > b - a).unwrap_or(true)
                && l.get(x0 + 1) == Some(&'─')
            {
                best = Some((y, x0, x1));
            }
        }
    }
    let Some((top, x0, x1)) = best else {
        return s.to_string();
    };
    let mut out = Vec::new();
    for l in lines.iter().skip(top + 1) {
        if l.get(x0) == Some(&'╰') {
            break;
        }
        let inner: String = l.iter().skip(x0 + 1).take(x1 - x0 - 1).collect();
        out.push(inner);
    }
    out.join("\n")
}

fn flat(s: &str) -> String {
    let s = &inside_modal(s);
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' ' || c == '┃'))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .replace("· ·", "·")
}

fn page(size: (i32, i32), accounts: &str) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(users_view));
    h.admin();
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&fx(accounts)).unwrap(),
    ));
    h.turns(3);
    h.sent();
    h
}

fn select(h: &mut r8w4::Harness, id: &str) {
    let idx = h.store.accounts.with_untracked(|d| {
        d.ready()
            .unwrap()
            .iter()
            .filter(|r| !r.archived)
            .position(|r| r.id == id)
    });
    h.ui.account_sel.set(idx.expect(id));
    h.turns(2);
}

fn gets(cmds: &[Cmd]) -> Vec<(String, String)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) => Some((key.clone(), path.clone())),
            _ => None,
        })
        .collect()
}

fn puts(cmds: &[Cmd]) -> Vec<(String, String, Value)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                method, path, body, ..
            }) => Some((method.clone(), path.clone(), body.clone())),
            _ => None,
        })
        .collect()
}

#[test]
fn every_row_has_preferences_and_p_opens_the_rows_route() {
    for (id, slot, path) in [
        (
            "alice",
            "prefs.default.alice",
            "/accounts/alice/preferences",
        ),
        (
            "castor",
            "prefs.default.castor",
            "/accounts/castor/preferences",
        ),
        ("admin", "prefs.default.admin", "/accounts/me/preferences"),
    ] {
        let mut h = page((120, 40), "accounts_w2");
        select(&mut h, id);
        // R15: the row's Preferences button (⊜, key p).
        assert!(flat(&h.turns(2)).contains("⊜"), "{id}");
        h.key(b"p");
        assert_eq!(
            gets(&h.sent()),
            vec![(slot.to_string(), path.to_string())],
            "{id}"
        );
        let s = h.turns(2);
        assert!(flat(&s).contains(&format!("Preferences — {id}")), "{s}");
        assert!(flat(&s).contains("Reading the preferences..."), "{s}");
    }
}

#[test]
fn the_modal_reads_like_the_web_and_a_pick_is_one_put() {
    for size in SIZES {
        let mut h = page(size, "accounts_w2");
        select(&mut h, "alice");
        h.key(b"p");
        h.sent();
        h.store.json.set(
            "prefs.default.alice",
            Loadable::Ready(fx("prefs_alice_set")),
        );
        let s = h.shoot("r14w3-preferences-alice");
        let f = flat(&s);
        assert!(
            f.contains("The workflow each app runs for alice unless a conversation picks another. Gateway default follows the admin's Default workflow per app."),
            "{s}"
        );
        // R15: each app is a label + a Select showing the current choice.
        let row =
            |label: &str, value: &str| s.lines().any(|l| l.contains(label) && l.contains(value));
        assert!(row("AbstractCode — chat agent", "CodeAct agent"), "{s}");
        assert!(
            row("Assistant", "Gateway default (AbstractAssistant"),
            "{s}"
        );
        h.assert_fits();
        // Enter opens the Select's list: the gateway default first.
        h.key(b"\r");
        let s = h.shoot("r14w3-preferences-alice-pick");
        let f = flat(&s);
        let gd = f.find("Gateway default (Basic agent)").expect(&s);
        let cur = f.rfind("CodeAct agent").expect(&s);
        assert!(gd < cur, "{s}");
        h.assert_fits();
        // ↑ to the gateway default, Enter: ONE PUT of that interface (null).
        h.key(UP);
        h.key(UP);
        h.key(b"\r");
        let w = puts(&h.sent());
        assert_eq!(
            w,
            vec![(
                "PUT".to_string(),
                "/accounts/alice/preferences".to_string(),
                json!({"default_workflow": {"abstractcode.agent.v1": null}})
            )]
        );
        h.store.json.set_write(
            "prefs.default.alice.write",
            Some(WriteState::Done(fx("prefs_alice_put"))),
        );
        let s = h.turns(3);
        assert!(flat(&s).contains("Saved."), "{s}");
    }
}

#[test]
fn a_refusal_is_not_saved_then_the_gateways_message() {
    let mut h = page((120, 40), "accounts_w2");
    select(&mut h, "alice");
    h.key(b"p");
    h.sent();
    h.store.json.set(
        "prefs.default.alice",
        Loadable::Ready(fx("prefs_alice_set")),
    );
    h.turns(2);
    h.key(b"\r");
    h.key(DOWN);
    h.key(b"\r");
    let w = puts(&h.sent());
    assert_eq!(w.len(), 1);
    assert_eq!(
        w[0].2,
        json!({"default_workflow": {"abstractcode.agent.v1": "coding-agent:coder"}})
    );
    let body = fx("prefs_alice_refused");
    h.store.json.set_write(
        "prefs.default.alice.write",
        Some(WriteState::Failed(ApiError {
            kind: ApiErrorKind::Http(400),
            message: body["detail"].to_string(),
            body: Some(body["detail"].clone()),
            timed_out: false,
        })),
    );
    let s = h.shoot("r14w3-preferences-refused");
    assert!(
        flat(&s).contains("Not saved. default_workflow.abstractcode.agent.v1 = 'nope:zzz' refused: workflow bundle 'nope' is not on this gateway."),
        "{s}"
    );
}

#[test]
fn an_older_gateway_has_no_preferences_and_p_says_so() {
    let mut h = page((120, 40), "accounts");
    select(&mut h, "alice");
    let _ = h.turns(2);
    h.key(b"p");
    assert!(gets(&h.sent()).is_empty());
    assert!(h
        .store
        .notice
        .get_untracked()
        .unwrap_or_default()
        .contains("does not offer per-account preferences"));
}

#[test]
fn a_load_failure_is_said() {
    let mut h = page((120, 40), "accounts_w2");
    select(&mut h, "alice");
    h.key(b"p");
    h.sent();
    h.store.json.set(
        "prefs.default.alice",
        Loadable::Failed(ApiError {
            kind: ApiErrorKind::Http(404),
            message: "Not Found".into(),
            body: Some(json!({"detail": "Not Found"})),
            timed_out: false,
        }),
    );
    let s = h.turns(3);
    assert!(
        flat(&s).contains("Could not read the preferences of alice: Not Found"),
        "{s}"
    );
}
