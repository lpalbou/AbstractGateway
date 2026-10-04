//! R8.1 Workflows (round 8): rows never expand; with "Older versions" on,
//! each older version is its own row with its own actions; descriptions
//! are edited in place by their owner (`actions.can_edit_description`;
//! PATCH /bundles/{id}); Export / Open / Archive are keys in ONE line.
//! Hermetic snapshots + key drives; live drive (ignored by default):
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_WORKFLOW=<an editable bundle id> R8W4_SHOTS_DIR=<dir> \
//!   cargo test --test r8w4_workflows -- --ignored --test-threads 1

mod r8w4;

use abstractgateway_console::store::workflows_page::workflows_from_payload;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, workflows};
use abstractgateway_console::worker::workflows::WfCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    workflows::view(cx, ctx, &t)
}

fn version(owner: &str, id: &str, name: &str, v: &str, source: &str, extra: Value) -> Value {
    let mut it = json!({"bundle_id": id, "bundle_version": v, "owner": {"kind": owner}, "is_draft": false,
        "archived": false, "available": true, "source": source, "description": format!("{name} does its job."),
        "default_entrypoint": "f1", "created_at": format!("2026-10-0{}T10:00:00Z", v.len() % 9 + 1), "version_channel": "",
        "entrypoints": [{"flow_id": "f1", "name": name, "description": "", "interfaces": []}],
        "actions": {"can_archive": source != "shipped", "can_set_availability": owner == "gateway",
                    "can_edit_description": source != "shipped"}});
    for (k, val) in extra.as_object().unwrap() {
        it[k] = val.clone();
    }
    it
}

fn bundles() -> Value {
    json!({"items": [
        version("gateway", "basic-agent", "Basic agent", "0.0.5", "shipped", json!({})),
        version("user", "note-taker", "Note taker", "1.2.0", "imported", json!({"description": "My own words.", "description_edited": true})),
        version("user", "note-taker", "Note taker", "1.1.0", "imported", json!({})),
    ], "skipped": []})
}

fn page(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .wf
        .data
        .set(Loadable::Ready(workflows_from_payload(&bundles()).unwrap()));
    h.turns(3);
    h
}

fn wf_cmds(h: &mut r8w4::Harness) -> Vec<WfCmd> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Workflows(w) => Some(w),
            _ => None,
        })
        .collect()
}

/// Highlight table row `row` (captions count).
fn select(h: &mut r8w4::Harness, row: usize) {
    h.store.wf.sel.set(row);
    h.turns(2);
}

#[test]
fn rows_do_not_expand_and_the_actions_are_one_line() {
    for size in SIZES {
        let mut h = page(size);
        select(&mut h, 3); // group, Basic agent, group, Note taker
        let s = h.key(b"\r");
        h.shoot("workflows-row");
        assert!(
            !s.contains("Actions:") && !s.contains("▸"),
            "nothing unfolds:\n{s}"
        );
        assert!(s.contains("Note taker"), "{s}");
        assert!(s.contains("x Export"), "{s}");
        assert!(
            s.contains("d Archive") && s.contains("e Edit description"),
            "{s}"
        );
        assert!(s.contains("My own words. (edited)"), "{s}");
        h.assert_fits();
    }
}

#[test]
fn older_versions_are_rows_with_their_own_archive() {
    let mut h = page((120, 40));
    h.key(b"o");
    let s = h.shoot("workflows-older");
    assert!(s.contains("↳ older version") && s.contains("1.1.0"), "{s}");
    select(&mut h, 4); // the 1.1.0 row under Note taker
    let s = h.text();
    assert!(s.contains("Note taker 1.1.0: x Export"), "{s}");
    assert!(
        !s.contains("e Edit description"),
        "one description per bundle:\n{s}"
    );
    h.sent();
    h.key(b"d");
    h.key(b"y");
    match wf_cmds(&mut h).as_slice() {
        [WfCmd::Archive {
            bundle_id, version, ..
        }] => {
            assert_eq!(bundle_id, "note-taker");
            assert_eq!(version, "1.1.0", "only that version");
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn e_edits_the_description_in_place_and_enter_saves() {
    for size in SIZES {
        let mut h = page(size);
        select(&mut h, 3);
        h.key(b"e");
        let s = h.shoot("workflows-description-editing");
        assert!(s.contains("Description of Note taker:"), "{s}");
        assert!(s.contains("Enter saves · Esc keeps"), "{s}");
        h.sent();
        // Clear and type: the input owns the keys (x/d/e do nothing here).
        h.key(b"\x1b[F"); // End: the caret starts at the beginning
        for _ in 0..20 {
            h.key(b"\x7f");
        }
        h.type_text("Takes notes, exports them.");
        h.key(b"\r");
        match wf_cmds(&mut h).as_slice() {
            [WfCmd::SetDescription {
                bundle_id,
                description,
                ..
            }] => {
                assert_eq!(bundle_id, "note-taker");
                assert_eq!(description, "Takes notes, exports them.");
            }
            other => panic!("PATCH /bundles/note-taker: {other:?}"),
        }
    }
}

#[test]
fn esc_keeps_the_description() {
    let mut h = page((80, 24));
    select(&mut h, 3);
    h.key(b"e");
    h.type_text(" more");
    let s = h.esc();
    assert!(!s.contains("Description of"), "{s}");
    assert!(wf_cmds(&mut h).is_empty());
    // The table has the keyboard again: ↑ moves the selection.
    h.key(b"\x1b[A");
    assert_eq!(
        h.store.wf.sel.get_untracked(),
        1,
        "keys reach the table again"
    );
}

#[test]
fn a_shipped_workflow_keeps_its_description() {
    let mut h = page((80, 24));
    select(&mut h, 1);
    let s = h.key(b"e");
    assert!(
        s.contains("Workflows that ship with the gateway keep their own description."),
        "{s}"
    );
    assert!(!s.contains("Description of"), "{s}");
}

/// Live: edit a description, read it back, put it back.
#[test]
#[ignore]
fn live_description_edit() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let bid = std::env::var("R8W4_WORKFLOW").expect("R8W4_WORKFLOW");
    let mut h = live((120, 40), Mount::Page(page_view), &url, &token);
    workflows::refresh_for_tests(&h.store, &h.tx);
    h.until("list", |h, _| {
        h.store
            .wf
            .data
            .with_untracked(|d| matches!(d, Loadable::Ready(_)))
    });
    // Pick the row by its bundle id in the actions line.
    let mut found = false;
    for i in 0..60 {
        h.store.wf.sel.set(i);
        let s = h.turns(2);
        if s.lines().any(|l| l.contains(&bid)) && s.contains("e Edit description") {
            found = true;
            break;
        }
    }
    assert!(found, "{bid} editable row");
    h.key(b"e");
    h.key(b"\x1b[F");
    for _ in 0..300 {
        h.key(b"\x7f");
    }
    h.type_text("Edited from the terminal (R8-W4).");
    h.key(b"\r");
    h.until_text("Saved the description");
    h.shoot("live-workflows-description-saved");
    let v = gw("GET", &url, &token, "/bundles?all_versions=true", None);
    let it = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["bundle_id"] == json!(bid))
        .cloned()
        .unwrap();
    assert_eq!(
        it["description"],
        json!("Edited from the terminal (R8-W4).")
    );
    assert_eq!(it["description_edited"], json!(true));
    // Back to the file's own description.
    h.key(b"e");
    h.key(b"\x1b[F");
    for _ in 0..300 {
        h.key(b"\x7f");
    }
    h.key(b"\r");
    h.until_text("Saved the description");
    let v = gw("GET", &url, &token, "/bundles?all_versions=true", None);
    let it = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["bundle_id"] == json!(bid))
        .cloned()
        .unwrap();
    assert_eq!(it["description_edited"], json!(false));
}
