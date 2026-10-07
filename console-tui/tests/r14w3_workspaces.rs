//! R14.3 (round 14): the terminal console's workspaces = the web console's
//! (DESIGN "R11.1 FINAL"). No Workspaces screen any more: Accounts carries
//! "Eligible workspaces" (admins, `E`) and a Workspaces action on EVERY row
//! (`w`: humans, entities, the admin's own as `me`), both the ONE chooser
//! (`ui::workspace_chooser`) with the kit's words. The command sandbox
//! state line sits under the Accounts head (R12.1). The old `W` key opens
//! Accounts.
//!
//! Hermetic: the recorded answers of the real routes (tests/fixtures/
//! r14w3_*.json, captured from a scratch gateway, the scratch prefix
//! rewritten to /srv/w3/) are put into the JSON lane's slots by hand; the
//! harness's command channel is the only way out of the UI, so the
//! commands it drains ARE the requests. Captures land in R8W4_SHOTS_DIR.
//!
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r14w3_workspaces

mod r8w4;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{runtimes_from_payload, Loadable};
use abstractgateway_console::ui::{self, runtimes, users, workspace_chooser};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};
use serde_json::{json, Value};

const UP: &[u8] = b"\x1b[A";
const DOWN: &[u8] = b"\x1b[B";
const RIGHT: &[u8] = b"\x1b[C";
const LEFT: &[u8] = b"\x1b[D";

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

fn runtimes_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    runtimes::view(cx, ctx, &t)
}

/// The text with borders and line breaks folded away.
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' ' || c == '┃'))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .replace("· ·", "·")
}

fn page(size: (i32, i32), admin: bool) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(users_view));
    if admin {
        h.admin();
    } else {
        h.identity("bob", false);
    }
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&fx("accounts")).unwrap(),
    ));
    h.store
        .workspace_policy
        .set(Loadable::Ready(fx("policy_allowed")));
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

/// The JSON-lane GETs the UI sent.
fn gets(cmds: &[Cmd]) -> Vec<(String, String)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) => Some((key.clone(), path.clone())),
            _ => None,
        })
        .collect()
}

/// The JSON-lane writes the UI sent: (method, path, body).
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

/// Open "Eligible workspaces" (`E`) and answer its GET with `fixture`.
fn open_gateway(h: &mut r8w4::Harness, fixture: &str) -> String {
    h.key(b"E");
    let sent = h.sent();
    assert_eq!(
        gets(&sent),
        vec![("ws.gateway".to_string(), "/workspace/policy".to_string())],
        "{sent:?}"
    );
    h.store.json.set("ws.gateway", Loadable::Ready(fx(fixture)));
    h.turns(3)
}

/// Open the selected row's Workspaces (`w`) and answer with `fixture`.
fn open_account(h: &mut r8w4::Harness, slot: &str, path: &str, fixture: &str) -> String {
    h.key(b"w");
    let sent = h.sent();
    assert_eq!(
        gets(&sent),
        vec![(slot.to_string(), path.to_string())],
        "{sent:?}"
    );
    h.store.json.set(slot, Loadable::Ready(fx(fixture)));
    h.turns(3)
}

#[test]
fn the_accounts_head_has_eligible_workspaces_and_the_sandbox_state_line() {
    for size in SIZES {
        let mut h = page(size, true);
        let s = h.shoot("r14w3-accounts-admin");
        let f = flat(&s);
        assert!(f.contains("E Eligible workspaces"), "{s}");
        assert!(f.contains("Commands sandboxed: macOS sandbox-exec"), "{s}");
        assert!(
            f.contains("Every command a run starts is confined by the operating system to that run's workspaces."),
            "{s}"
        );
        assert!(!f.contains("follows in the next update"), "{s}");
        h.assert_fits();
        // A non-admin: no Eligible workspaces, the state line all the same.
        let mut h = page(size, false);
        let s = h.shoot("r14w3-accounts-user");
        assert!(!s.contains("Eligible workspaces"), "{s}");
        assert!(
            flat(&s).contains("Commands sandboxed: macOS sandbox-exec"),
            "{s}"
        );
        // E refuses with the reason, sends nothing.
        h.key(b"E");
        assert!(gets(&h.sent()).is_empty());
    }
}

#[test]
fn every_row_humans_and_entities_has_the_workspaces_action() {
    let mut h = page((120, 40), true);
    for id in ["admin", "alice", "bob", "castor"] {
        select(&mut h, id);
        let s = h.turns(2);
        assert!(
            flat(&s).contains(&format!("{id}:")) && flat(&s).contains("w Workspaces"),
            "{id}:\n{s}"
        );
    }
    select(&mut h, "castor");
    let s = h.turns(2);
    assert!(
        flat(&s).contains("castor: @ Email · l Logs · w Workspaces · m Manage · d Archive"),
        "{s}"
    );
}

#[test]
fn w_on_an_entity_opens_its_workspaces_following_the_gateway() {
    for size in SIZES {
        let mut h = page(size, true);
        select(&mut h, "castor");
        let s = open_account(
            &mut h,
            "ws.account.default:castor",
            "/workspace/policy/default%3Acastor",
            "account_castor_follow",
        );
        let s2 = h.shoot("r14w3-account-castor-follow");
        let f = flat(&s2);
        assert!(f.contains("Workspaces — castor"), "{s}");
        assert!(
            f.contains("The workspaces this account's agents use, among the eligible ones."),
            "{s2}"
        );
        assert!(
            f.contains("Gateway: Deny everything, allow listed workspaces · /srv/w3/ws/proj (rw) · /srv/w3/ws/archive (ro)"),
            "{s2}"
        );
        assert!(f.contains("[x] Follow the gateway policy"), "{s2}");
        assert!(
            f.contains("On: this account gets exactly what the gateway allows."),
            "{s2}"
        );
        // Following: what applies, read-only (no add row, no controls).
        assert!(!f.contains("Add a workspace path"), "{s2}");
        assert!(!f.contains("Permission:"), "{s2}");
        h.assert_fits();
    }
}

#[test]
fn the_own_row_uses_me() {
    let mut h = page((120, 40), true);
    select(&mut h, "admin");
    open_account(
        &mut h,
        "ws.account.me",
        "/workspace/policy/me",
        "account_castor_follow",
    );
}

#[test]
fn eligible_workspaces_shows_posture_caps_builtins_and_the_ceiling_line() {
    for size in SIZES {
        let mut h = page(size, true);
        open_gateway(&mut h, "policy_allowed");
        let s = h.shoot("r14w3-eligible-top");
        let f = flat(&s);
        assert!(f.contains("Eligible workspaces"), "{s}");
        assert!(
            f.contains("The workspaces accounts may choose from, and the most each one allows."),
            "{s}"
        );
        assert!(f.contains("Workspaces agents may use"), "{s}");
        assert!(
            f.contains("(•) Deny everything, allow listed workspaces"),
            "{s}"
        );
        assert!(
            f.contains("( ) Allow everything, refuse listed workspaces"),
            "{s}"
        );
        assert!(
            f.contains("Agents may only work in the listed workspaces."),
            "{s}"
        );
        assert!(f.contains("Allowed workspaces"), "{s}");
        assert!(
            f.contains("Permission: [Read & write] Read-only Refused"),
            "{s}"
        );
        assert!(
            !f.contains("Gateway: "),
            "no gateway line at the gateway level:\n{s}"
        );
        h.assert_fits();
        // Down to the add row: the view scrolls with the selection; the
        // built-in refusals are listed, fixed, and the ceiling line closes.
        for _ in 0..4 {
            h.key(DOWN);
        }
        let s = h.shoot("r14w3-eligible-bottom");
        let f = flat(&s);
        assert!(f.contains("+ Add a workspace path"), "{s}");
        assert!(
            f.contains("Deny everything, allow listed workspaces · /srv/w3/ws/proj (rw) · /srv/w3/ws/archive (ro)"),
            "{s}"
        );
        h.assert_fits();
    }
    let mut h = page((120, 40), true);
    open_gateway(&mut h, "policy_allowed");
    h.wheel_down(10);
    let f = flat(&h.turns(2));
    assert!(f.contains("Refused workspaces"), "{f}");
    assert!(
        f.contains("Refused — Always refused: the gateway's own data and credentials"),
        "{f}"
    );
}

#[test]
fn every_change_is_one_put_of_the_whole_level() {
    let mut h = page((120, 40), true);
    open_gateway(&mut h, "policy_allowed");
    // The posture: → = "Allow everything, refuse listed workspaces".
    h.key(RIGHT);
    let w = puts(&h.sent());
    assert_eq!(
        w,
        vec![(
            "PUT".to_string(),
            "/workspace/policy".to_string(),
            json!({"posture": "any_except_denied", "default_mode": "rw", "folders": [
                {"path": "/srv/w3/ws/proj", "mode": "rw"}, {"path": "/srv/w3/ws/archive", "mode": "ro"}]})
        )]
    );
    // While the write is in flight, nothing else is sent.
    h.key(RIGHT);
    assert!(puts(&h.sent()).is_empty());
    // The answer lands: "Saved" beside the posture.
    h.store.json.set_write(
        "ws.gateway.write",
        Some(WriteState::Done(fx("policy_put_any"))),
    );
    h.store
        .json
        .set("ws.gateway", Loadable::Ready(fx("policy_any")));
    let s = h.turns(3);
    let f = flat(&s);
    assert!(f.contains("Saved"), "{s}");
    assert!(
        f.contains("(•) Allow everything, refuse listed workspaces"),
        "{s}"
    );
    assert!(
        f.contains("Agents may work in any workspace except the refused ones."),
        "{s}"
    );
    // A row: ↓↓ to the refused row (archive is listed first, under
    // Allowed), → nothing past Refused; ← = Read-only.
    h.key(DOWN);
    h.key(DOWN);
    h.key(RIGHT);
    assert!(puts(&h.sent()).is_empty(), "Refused is the last option");
    h.key(LEFT);
    let w = puts(&h.sent());
    assert_eq!(w.len(), 1);
    assert_eq!(
        w[0].2["folders"],
        json!([{"path": "/srv/w3/ws/secret", "mode": "ro"}, {"path": "/srv/w3/ws/archive", "mode": "ro"}])
    );
    h.store
        .json
        .set_write("ws.gateway.write", Some(WriteState::Done(fx("policy_any"))));
    h.turns(2);
    // x removes the selected row.
    h.key(b"x");
    let w = puts(&h.sent());
    assert_eq!(
        w[0].2["folders"],
        json!([{"path": "/srv/w3/ws/archive", "mode": "ro"}])
    );
    h.store
        .json
        .set_write("ws.gateway.write", Some(WriteState::Done(fx("policy_any"))));
    h.turns(2);
    // Everything else (below the 12 built-in refusals: the view scrolls
    // with the selection): ← is Read & write (current), → Read-only.
    h.key(DOWN);
    let s = h.shoot("r14w3-eligible-everything-else");
    assert!(flat(&s).contains("▸ Everything else"), "{s}");
    assert!(
        flat(&s).contains("Permission: [Read & write] Read-only"),
        "{s}"
    );
    h.key(RIGHT);
    let w = puts(&h.sent());
    assert_eq!(w[0].2["default_mode"], "ro");
    h.store
        .json
        .set_write("ws.gateway.write", Some(WriteState::Done(fx("policy_any"))));
    h.turns(2);
    // The add row: Enter, type, Enter → the path appended, refused under
    // posture b = a refused row.
    h.key(DOWN);
    h.key(b"\r");
    h.type_text("/srv/w3/ws/new");
    h.key(b"\r");
    let w = puts(&h.sent());
    assert_eq!(
        w[0].2["folders"][2],
        json!({"path": "/srv/w3/ws/new", "mode": "deny"}),
        "{w:?}"
    );
}

#[test]
fn a_refusal_shows_the_gateways_sentence_and_not_saved() {
    for size in SIZES {
        let mut h = page(size, true);
        open_gateway(&mut h, "policy_allowed");
        for _ in 0..3 {
            h.key(DOWN);
        }
        h.key(b"\r");
        h.type_text("/nonexistent/zzz");
        h.key(b"\r");
        assert_eq!(puts(&h.sent()).len(), 1);
        let body = fx("policy_refused_nonexistent");
        h.store.json.set_write(
            "ws.gateway.write",
            Some(WriteState::Failed(ApiError {
                kind: ApiErrorKind::Http(400),
                message: body["detail"].to_string(),
                body: Some(body["detail"].clone()),
                timed_out: false,
            })),
        );
        let s = h.shoot("r14w3-eligible-refusal");
        assert!(
            flat(&s).contains("Workspaces entry '/nonexistent/zzz': No directory at this path on the gateway's computer. Not saved."),
            "{s}"
        );
        h.assert_fits();
    }
}

#[test]
fn a_mode_above_the_cap_is_refused_with_the_kits_sentence_and_nothing_sent() {
    for size in SIZES {
        let mut h = page(size, true);
        select(&mut h, "alice");
        open_account(
            &mut h,
            "ws.account.default:alice",
            "/workspace/policy/default%3Aalice",
            "account_alice_configured",
        );
        let s = h.shoot("r14w3-account-alice-configured");
        let f = flat(&s);
        assert!(f.contains("[ ] Follow the gateway policy"), "{s}");
        assert!(
            f.contains("Gateway: Deny everything, allow listed workspaces"),
            "{s}"
        );
        // The archive row (cap ro): Read & write is marked unavailable and
        // its reason is printed under the row.
        assert!(
            f.contains("Read & write: The gateway allows this workspace read-only"),
            "{s}"
        );
        // ↓ Follow → Posture → the archive row; ← = Read & write: refused.
        h.key(DOWN);
        h.key(DOWN);
        h.key(LEFT);
        assert!(puts(&h.sent()).is_empty(), "above the cap: nothing sent");
        assert_eq!(
            h.store.notice.get_untracked().as_deref(),
            Some("Read & write: The gateway allows this workspace read-only")
        );
        h.turns(2);
        // → = Refused: allowed (a deny never widens).
        h.key(RIGHT);
        let w = puts(&h.sent());
        assert_eq!(w.len(), 1);
        assert_eq!(w[0].1, "/workspace/policy/default%3Aalice");
        assert_eq!(w[0].2["configured"], true);
        assert_eq!(
            w[0].2["folders"][0],
            json!({"path": "/srv/w3/ws/archive", "mode": "deny"})
        );
        h.assert_fits();
    }
}

#[test]
fn the_follow_switch_is_one_put() {
    let mut h = page((120, 40), true);
    select(&mut h, "alice");
    open_account(
        &mut h,
        "ws.account.default:alice",
        "/workspace/policy/default%3Aalice",
        "account_alice_configured",
    );
    h.key(b" ");
    let w = puts(&h.sent());
    assert_eq!(
        w,
        vec![(
            "PUT".to_string(),
            "/workspace/policy/default%3Aalice".to_string(),
            json!({"configured": false})
        )]
    );
    h.store.json.set_write(
        "ws.account.default:alice.write",
        Some(WriteState::Done(fx("account_alice_follow"))),
    );
    h.store.json.set(
        "ws.account.default:alice",
        Loadable::Ready(fx("account_alice_follow")),
    );
    let s = h.turns(3);
    assert!(flat(&s).contains("[x] Follow the gateway policy"), "{s}");
    // OFF again: the effective answer, verbatim, as the account's rows.
    h.key(b" ");
    let w = puts(&h.sent());
    assert_eq!(w[0].2["configured"], true);
    assert_eq!(w[0].2["posture"], "allowed_only");
    assert_eq!(
        w[0].2["folders"],
        json!([{"path": "/srv/w3/ws/proj", "mode": "rw"}, {"path": "/srv/w3/ws/archive", "mode": "ro"}])
    );
}

#[test]
fn a_load_failure_or_an_older_answer_is_said_never_blank() {
    let mut h = page((120, 40), true);
    h.key(b"E");
    h.sent();
    h.store.json.set(
        "ws.gateway",
        Loadable::Ready(
            json!({"ok": true, "policy": {"posture": "allowed_only", "shared_workspace": "/x"}}),
        ),
    );
    let s = h.turns(3);
    assert!(
        flat(&s).contains("The eligible workspaces could not be loaded: The gateway answered with an older workspace model"),
        "{s}"
    );
}

#[test]
fn the_old_workspaces_key_opens_accounts_and_no_workspaces_screen_is_left() {
    for size in SIZES {
        let mut h = harness(size, Mount::Root);
        h.admin();
        h.turns(3);
        h.key(b"5");
        assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_RUNTIMES);
        h.key(b"W");
        let s = h.turns(3);
        assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_USERS, "{s}");
        assert!(!s.contains("W Workspaces"), "{s}");
        assert!(!s.contains("follows in the next update"), "{s}");
    }
    assert!(!ui::SCREENS.contains(&"Workspaces"));
    assert!(!ui::SCREEN_IDS.contains(&"workspaces"));
}

/// No surface of the crate still says the parked sentence (red when it
/// comes back anywhere in src/).
#[test]
fn the_parked_sentence_is_gone_from_the_sources() {
    fn walk(dir: &std::path::Path, hits: &mut Vec<String>) {
        for e in std::fs::read_dir(dir).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                walk(&p, hits);
            } else if p.extension().is_some_and(|x| x == "rs") {
                let s = std::fs::read_to_string(&p).unwrap();
                if s.contains("follows in the next update") {
                    hits.push(p.display().to_string());
                }
            }
        }
    }
    let mut hits = Vec::new();
    walk(
        &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut hits,
    );
    assert!(hits.is_empty(), "the parked sentence is still in {hits:?}");
}

#[test]
fn runtimes_w_opens_the_planes_workspaces() {
    let mut h = harness((120, 40), Mount::Page(runtimes_view));
    h.admin();
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&fx("runtimes"))));
    h.turns(3);
    let s = h.shoot("r14w3-runtimes");
    assert!(flat(&s).contains("w: Eligible workspaces"), "{s}");
    h.sent();
    h.ui.runtime_sel.set(0);
    h.turns(2);
    h.key(b"w");
    assert_eq!(
        gets(&h.sent()),
        vec![("ws.gateway".to_string(), "/workspace/policy".to_string())]
    );
    h.esc();
    h.ui.runtime_sel.set(3);
    h.turns(2);
    h.key(b"w");
    assert_eq!(
        gets(&h.sent()),
        vec![(
            "ws.account.default:castor".to_string(),
            "/workspace/policy/default%3Acastor".to_string()
        )]
    );
}

#[test]
fn the_wording_table_is_the_kits() {
    // The crate's table is diffed against the recorded kit table in the
    // module's own unit test; here the screens' words come FROM it.
    let t: std::collections::HashMap<_, _> = workspace_chooser::TABLE.into_iter().collect();
    assert_eq!(
        t["postureAllowedOnly"],
        "Deny everything, allow listed workspaces"
    );
    assert_eq!(
        t["postureAnyExceptDenied"],
        "Allow everything, refuse listed workspaces"
    );
    assert_eq!(t["allowedTitle"], "Allowed workspaces");
    assert_eq!(t["deniedTitle"], "Refused workspaces");
    assert_eq!(
        t["capReadOnly"],
        "The gateway allows this workspace read-only"
    );
    let _ = UP;
}
