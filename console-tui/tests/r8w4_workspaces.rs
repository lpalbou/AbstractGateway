//! R8.2 Workspaces (round 8): its own page right after Accounts — the
//! gateway policy and the per-account policies; the editor overlay with
//! the segmented access mode, the launch-folder switch, the folder rows
//! (in place, path-checked) and "Follow the gateway policy". Hermetic
//! snapshots + key drives (the UI's commands are asserted), and live
//! drives against a scratch gateway (ignored by default):
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r8w4_workspaces -- --ignored --test-threads 1

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::operator::MyPolicy;
use abstractgateway_console::store::workspaces::{Edit, ListKind, Scope};
use abstractgateway_console::store::{Loadable, RuntimeConfigData};
use abstractgateway_console::ui::{self, workspaces};
use abstractgateway_console::worker::workspaces::WsCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    workspaces::view(cx, ctx, &t)
}

fn config() -> Value {
    json!({"writable": true,
        "workspace_default_mode": {"value": "whitelist", "source": "default"},
        "trust_client_launch_folder": {"value": true, "source": "default"},
        "workspace_allowed_paths": {"value": "/srv/projects\n/srv/shared", "source": "stored"},
        "workspace_blocked_paths": {"value": "/etc", "source": "stored"},
        "user_workspace_policies": {"value": {"default:alice": {"mode": "blacklist",
            "workspace_blocked_paths": ["/home/alice/private"], "client_workspace_scope_overrides": true}},
            "source": "stored"}})
}

fn accounts() -> Value {
    json!({"accounts": [
        accounts_fixture::user_row("admin", true, "", "", true, true),
        accounts_fixture::user_row("alice", false, "alice@example.test", "", true, false),
        accounts_fixture::user_row("bob", false, "", "", true, false),
        accounts_fixture::entity_row("castor", "awake", true),
    ]})
}

fn page(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(&config())));
    h.store
        .accounts
        .set(Loadable::Ready(accounts_from_payload(&accounts()).unwrap()));
    h.turns(3);
    h
}

fn sent_ws(h: &mut r8w4::Harness) -> Vec<WsCmd> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Workspaces(w) => Some(w),
            _ => None,
        })
        .collect()
}

#[test]
fn the_page_lists_the_gateway_policy_and_every_user_account() {
    for size in SIZES {
        let mut h = page(size);
        let s = h.shoot("workspaces");
        let f = flat(&s);
        assert!(
            f.contains("Workspaces — Which folders agents may read and write"),
            "{s}"
        );
        // The web's summary line: the gateway sentence + the own count.
        assert!(
            f.contains("Agents may use only 2 allowed folders and the folder they start in; 1 folder refused. 1 account has its own policy."),
            "{s}"
        );
        assert!(
            f.contains("[ ] Own policy · Follows the gateway policy."),
            "bob/admin:\n{s}"
        );
        assert!(
            f.contains("[x] Own policy · Any folder (old clients) is on for this account."),
            "alice:\n{s}"
        );
        // Entities set their folders in Manage — no row here.
        assert!(!s.contains("castor"), "{s}");
        h.assert_fits();
    }
}

/// The page's text with borders and line breaks folded away.
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' '))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

#[test]
fn the_accounts_jump_selects_that_accounts_row() {
    let mut h = page((80, 24));
    workspaces_focus(&mut h, "bob");
    assert_eq!(h.store.ws.sel.get_untracked(), 3);
    assert_eq!(h.store.ws.focus.get_untracked(), None, "consumed");
}

fn workspaces_focus(h: &mut r8w4::Harness, user: &str) {
    h.store
        .ws
        .focus
        .set(Some(("default".into(), user.to_string())));
    h.turns(3);
}

#[test]
fn enter_opens_the_gateway_editor_with_the_segmented_mode_and_rows() {
    for size in SIZES {
        let mut h = page(size);
        let s = h.key(b"\r");
        h.shoot("workspaces-editor-gateway");
        assert!(s.contains("Workspace policy — Gateway policy"), "{s}");
        assert!(
            s.contains("[Allow my list]  Allow everything except"),
            "{s}"
        );
        assert!(s.contains("[x] Launch-folder trust"), "{s}");
        if size.1 >= 40 {
            assert!(
                s.contains("Default folder") && s.contains("[ ] Any folder (old clients)"),
                "{s}"
            );
        }
        assert!(
            s.contains("Allowed folders") && s.contains("/srv/projects"),
            "{s}"
        );
        assert!(s.contains("Refused folders") && s.contains("/etc"), "{s}");
        assert!(s.contains("+ Add folder"), "{s}");
        // The gateway policy has no "Own policy" switch.
        assert!(!s.contains("Own policy"), "{s}");
        // No Save button: rows apply at once.
        assert!(!s.contains("Save"), "{s}");
        h.assert_fits();
    }
}

#[test]
fn the_mode_and_the_trust_switch_apply_at_once() {
    let mut h = page((120, 40));
    h.key(b"\r");
    h.sent();
    h.key(b" ");
    let cmds = sent_ws(&mut h);
    match cmds.as_slice() {
        [WsCmd::Save {
            scope: Scope::Gateway,
            before,
            policy: Some(after),
            ..
        }] => {
            assert_eq!(before.mode.as_deref(), Some("whitelist"));
            assert_eq!(after.mode.as_deref(), Some("blacklist"));
            assert_eq!(
                abstractgateway_console::worker::workspaces::gateway_body(before, after),
                json!({"workspace_default_mode": "blacklist"})
            );
        }
        other => panic!("space on Access saves the mode: {other:?}"),
    }
    let s = h.text();
    assert!(s.contains("Saving..."), "{s}");
    // The worker answers; the trust switch next.
    h.store.ws.busy.set(false);
    h.key(b"\x1b[B");
    h.key(b" ");
    let cmds = sent_ws(&mut h);
    match cmds.as_slice() {
        [WsCmd::Save {
            policy: Some(after),
            ..
        }] => assert_eq!(after.trust, Some(false)),
        other => panic!("space on Trust saves it: {other:?}"),
    }
}

#[test]
fn a_folder_is_added_in_place_and_checked_by_the_gateway() {
    let mut h = page((80, 24));
    h.key(b"\r");
    // Mode, Trust, /srv/projects, /srv/shared, + Add → 4 downs.
    for _ in 0..4 {
        h.key(b"\x1b[B");
    }
    h.key(b"\r");
    let s = h.shoot("workspaces-editor-adding");
    assert!(s.contains("Folder:"), "{s}");
    h.sent();
    h.type_text("/srv/new");
    h.key(b"\r");
    let cmds = sent_ws(&mut h);
    match cmds.as_slice() {
        [WsCmd::PutFolder {
            scope: Scope::Gateway,
            edit: Edit::Add(ListKind::Allowed),
            path,
            ..
        }] => {
            assert_eq!(path, "/srv/new")
        }
        other => panic!("Enter sends the path check + write: {other:?}"),
    }
    let s = h.text();
    assert!(s.contains("Checking…"), "{s}");
}

#[test]
fn esc_in_the_folder_input_keeps_the_overlay_open() {
    let mut h = page((80, 24));
    h.key(b"\r");
    for _ in 0..4 {
        h.key(b"\x1b[B");
    }
    h.key(b"\r");
    h.type_text("/nope");
    let s = h.esc();
    assert!(
        s.contains("Workspace policy — Gateway policy"),
        "still open:\n{s}"
    );
    assert!(!s.contains("Folder:"), "the input closed:\n{s}");
    assert!(sent_ws(&mut h).is_empty(), "nothing written");
    let s = h.esc();
    assert!(
        !s.contains("Workspace policy — Gateway policy"),
        "second Esc closes:\n{s}"
    );
}

#[test]
fn x_removes_a_folder_row() {
    let mut h = page((80, 24));
    h.key(b"\r");
    h.key(b"\x1b[B");
    h.key(b"\x1b[B"); // /srv/projects
    h.sent();
    h.key(b"x");
    match sent_ws(&mut h).as_slice() {
        [WsCmd::Save {
            policy: Some(after),
            before,
            ..
        }] => {
            assert_eq!(after.allowed, vec!["/srv/shared".to_string()]);
            assert_eq!(
                abstractgateway_console::worker::workspaces::gateway_body(before, after),
                json!({"workspace_allowed_paths": ["/srv/shared"]})
            );
        }
        other => panic!("x removes the row: {other:?}"),
    }
}

#[test]
fn own_policy_off_asks_to_drop_and_on_starts_from_the_gateway() {
    let mut h = page((120, 40));
    h.store.ws.sel.set(2); // alice (own policy)
    h.turns(2);
    let s = h.key(b"\r");
    h.shoot("workspaces-editor-account");
    assert!(s.contains("Workspace policy — alice's policy"), "{s}");
    assert!(s.contains("[x] Own policy"), "{s}");
    assert!(
        s.contains(" Allow my list  [Allow everything except]"),
        "{s}"
    );
    assert!(s.contains("/home/alice/private"), "{s}");
    assert!(s.contains("[x] Any folder (old clients)"), "{s}");
    assert!(!s.contains("Default folder"), "gateway only:\n{s}");
    h.sent();
    let s = h.key(b" "); // Own policy is the first line
    assert!(
        flat(&s).contains("Drop alice's own policy? Their agents follow the gateway policy again."),
        "{s}"
    );
    assert!(sent_ws(&mut h).is_empty(), "nothing before y");
    h.key(b"y");
    match sent_ws(&mut h).as_slice() {
        [WsCmd::Save {
            scope: Scope::Account { user_id, .. },
            policy: None,
            ..
        }] => {
            assert_eq!(user_id, "alice")
        }
        other => panic!("y sends {{policy: null}}: {other:?}"),
    }
    // bob follows the gateway: Own policy on copies its mode and trust.
    let mut h = page((120, 40));
    h.store.ws.sel.set(3);
    h.turns(2);
    let s = h.key(b"\r");
    assert!(s.contains("[ ] Own policy"), "{s}");
    assert!(
        !s.contains("Allowed folders"),
        "nothing else while off:\n{s}"
    );
    h.sent();
    h.key(b" ");
    match sent_ws(&mut h).as_slice() {
        [WsCmd::Save {
            policy: Some(after),
            ..
        }] => {
            assert_eq!(
                after.entry(),
                json!({"mode": "whitelist", "trust_client_launch_folder": true})
            )
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn an_account_edit_keeps_the_admin_grant() {
    let mut h = page((120, 40));
    h.store.ws.sel.set(2);
    h.turns(2);
    h.key(b"\r");
    h.sent();
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    h.key(b" "); // trust → explicit off
    match sent_ws(&mut h).as_slice() {
        [WsCmd::Save {
            policy: Some(after),
            ..
        }] => {
            let e = after.entry();
            assert_eq!(e["client_workspace_scope_overrides"], json!(true), "{e}");
            assert_eq!(e["trust_client_launch_folder"], json!(false), "{e}");
            assert_eq!(e["mode"], json!("blacklist"), "{e}");
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_non_admin_sees_one_line_and_edits_their_own_policy() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.identity("bob", false);
    h.store.op.my_policy.set(Loadable::Ready(MyPolicy::from_value(&json!({
        "tenant_id": "default", "user_id": "bob", "policy": {}, "customized": false,
        "effective": {"mode": "whitelist", "trust_client_launch_folder": true,
                      "workspace_allowed_paths": ["/srv/projects"], "workspace_blocked_paths": []}}))));
    let s = h.shoot("workspaces-non-admin");
    assert!(
        flat(&s).contains("The gateway policy: your agents may use only the gateway's allowed folders, the folder they start in."),
        "{s}"
    );
    assert!(!s.contains("alice"), "no per-account list:\n{s}");
    let s = h.key(b"\r");
    assert!(s.contains("Workspace policy — Your policy"), "{s}");
    assert!(s.contains("[ ] Own policy"), "{s}");
    h.sent();
    h.key(b" ");
    match sent_ws(&mut h).as_slice() {
        [WsCmd::Save {
            scope: Scope::Own,
            policy: Some(after),
            ..
        }] => {
            assert_eq!(after.mode.as_deref(), Some("whitelist"));
            assert_eq!(after.legacy, None, "self-service never sends the grant");
        }
        other => panic!("{other:?}"),
    }
}

// ------------------------------------------------------------------ live

fn live_page(size: (i32, i32)) -> Option<(r8w4::Harness, String, String)> {
    let (url, token) = live_env()?;
    let mut h = live(size, Mount::Page(page_view), &url, &token);
    workspaces::refresh_for_tests(&h.store, &h.tx);
    h.until_text("Own policy ·");
    Some((h, url, token))
}

/// Live: the mode applies at once and the gateway shows it; back again.
#[test]
#[ignore]
fn live_gateway_mode_and_folder_rows() {
    let Some((mut h, url, token)) = live_page((120, 40)) else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    // A folder made fresh for this run under R8W4_FOLDER (an existing base
    // folder): a run that stopped half-way never leaves this run a
    // duplicate ("Already in this list").
    let base = std::env::var("R8W4_FOLDER").expect("R8W4_FOLDER (an existing base folder)");
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let folder = format!("{}/r8w4-run-{stamp}", base.trim_end_matches('/'));
    std::fs::create_dir_all(&folder).expect("create the run's folder");
    h.key(b"\r");
    h.until_text("Workspace policy — Gateway policy");
    h.key(b" ");
    h.until_text("Saved");
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(
        cfg["workspace_default_mode"]["value"],
        json!("blacklist"),
        "{cfg}"
    );
    h.shoot("live-workspaces-mode-saved");
    h.store.ws.msg.set(None);
    h.key(b" ");
    h.until_text("Saved");
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(
        cfg["workspace_default_mode"]["value"],
        json!("whitelist"),
        "{cfg}"
    );
    // A refused folder: not saved, the gateway's sentence shown.
    let n_allowed = cfg["workspace_allowed_paths"]["paths"]
        .as_array()
        .map(Vec::len)
        .unwrap_or(0);
    for _ in 0..(2 + n_allowed) {
        h.key(b"\x1b[B");
    }
    h.key(b"\r");
    h.type_text("relative/path");
    h.key(b"\r");
    let s = h.until_text("Not saved.");
    h.shoot("live-workspaces-folder-refused");
    assert!(s.contains("Folder:"), "the input stays open:\n{s}");
    let after = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(
        after["workspace_allowed_paths"],
        cfg["workspace_allowed_paths"]
    );
    // A real folder: saved with the normalized path.
    for _ in 0.."relative/path".len() {
        h.key(b"\x7f");
    }
    h.type_text(&folder);
    h.key(b"\r");
    h.until("saved folder", |h, s| {
        s.contains("Saved")
            && !s.contains("Folder:")
            && h.store.ws.editing.get_untracked().is_none()
    });
    h.shoot("live-workspaces-folder-saved");
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    let paths: Vec<String> = cfg["workspace_allowed_paths"]["paths"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|p| p.as_str().map(str::to_string))
        .collect();
    assert!(
        paths
            .iter()
            .any(|p| p.ends_with(folder.trim_end_matches('/'))),
        "{paths:?}"
    );
    // The same folder again: the web's "Already in this list. Not saved."
    h.store.ws.msg.set(None);
    h.store.ws.item.set(3 + paths.len()); // + Add folder under the rows
    h.turns(2);
    h.key(b"\r");
    h.type_text(&folder);
    h.key(b"\r");
    h.until_text("Already in this list. Not saved.");
    h.esc();
    // Remove it again (x on its row).
    let idx = paths.len() - 1;
    h.store.ws.item.set(3 + idx); // Mode, Trust, the caption, then the rows
    h.store.ws.msg.set(None);
    h.key(b"x");
    h.until_text("Saved");
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(
        cfg["workspace_allowed_paths"]["paths"]
            .as_array()
            .map(Vec::len)
            .unwrap_or(0),
        n_allowed
    );
    let _ = std::fs::remove_dir(&folder);
}

/// Live: an account gets its own policy, then follows the gateway again.
#[test]
#[ignore]
fn live_account_policy_and_follow_the_gateway() {
    let Some((mut h, url, token)) = live_page((120, 40)) else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let user = std::env::var("R8W4_USER").unwrap_or_else(|_| "alice".into());
    h.until_text(&format!("{user} "));
    workspaces_focus(&mut h, &user);
    h.key(b"\r");
    h.until_text(&format!("Workspace policy — {user}'s policy"));
    h.key(b" "); // Own policy on: a copy of the gateway's mode and trust
    h.until_text("Allowed folders");
    let read = |url: &str, token: &str| {
        gw(
            "GET",
            url,
            token,
            &format!("/admin/user-workspace-policy?tenant_id=default&user_id={user}"),
            None,
        )
    };
    let v = read(&url, &token);
    assert!(v["policy"]["mode"].is_string(), "{v}");
    h.shoot("live-workspaces-account-own");
    // Own policy off: asks, then drops.
    h.store.ws.item.set(0);
    h.turns(2);
    h.key(b" ");
    h.until_text("Drop");
    h.key(b"y");
    h.until("follows again", |h, _| {
        h.store.runtime_config.with_untracked(|c| match c {
            Loadable::Ready(d) => !d
                .user_workspace_policies
                .contains(&format!("default:{user}")),
            _ => false,
        })
    });
    let v = read(&url, &token);
    assert!(v["policy"].is_null() || v["policy"] == json!({}), "{v}");
}
