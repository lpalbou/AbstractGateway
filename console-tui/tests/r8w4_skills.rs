//! R8.1 Skills tab (round 8): no "Shelf folder" panel — ONE inline row
//! under the list (folder edited in place + "Refresh curated shelf").
//! Hermetic snapshots + key drives (the UI's commands are asserted), and a
//! live drive against a scratch gateway (ignored by default):
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r8w4_skills -- --ignored --test-threads 1

mod r8w4;

use abstractgateway_console::store::skills::skills_from_payload;
use abstractgateway_console::store::{Loadable, RuntimeConfigData};
use abstractgateway_console::ui::skills_mcp;
use abstractgateway_console::worker::operator::OpCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn skills_payload() -> Value {
    json!({"skills": [
        {"name": "field-guide", "description": "Answers questions about the field guide.", "origin": "curated",
         "source_label": "Shelf folder", "version": "2026.10.04", "editable": false, "archived": false,
         "trust_level": "first_party", "blocked": false, "reasons": []}
    ], "warnings": []})
}

fn config(source: &str, value: &str) -> Value {
    json!({"writable": true, "skills": {"shelf": {
        "key": "skills.shelf", "value": value, "source": source, "resolved": if value.is_empty() {"/data/skills"} else {value},
        "available": true, "reason": "", "default_path": "/data/skills", "bundled_version": "2026.10.04"}}})
}

fn page(size: (i32, i32), source: &str, value: &str) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(skills_mcp::screen));
    h.admin();
    h.store.skills.skills.set(Loadable::Ready(
        skills_from_payload(&skills_payload()).unwrap(),
    ));
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(&config(
            source, value,
        ))));
    h.turns(3);
    h
}

#[test]
fn the_shelf_is_one_inline_row_under_the_list() {
    for size in SIZES {
        let mut h = page(size, "seeded", "");
        let s = h.shoot("skills-shelf-row");
        assert!(
            s.contains("Shelf folder: (the gateway's own copy) /data/skills"),
            "{s}"
        );
        assert!(s.contains("u Refresh curated shelf"), "{s}");
        assert!(s.contains("(version 2026.10.04)"), "{s}");
        // No disclosure, no separate panel.
        assert!(
            !s.contains("Shelf folder ▸") && !s.contains("Edit skills shelf"),
            "{s}"
        );
        // Below the list.
        let list = s.find("field-guide").unwrap();
        let row = s.find("Shelf folder:").unwrap();
        assert!(row > list, "the row sits under the list:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn a_saved_folder_shows_its_source_words() {
    let mut h = page((120, 40), "stored", "/srv/skills");
    let s = h.text();
    assert!(
        s.contains("Shelf folder: /srv/skills · Saved setting"),
        "{s}"
    );
}

#[test]
fn f_edits_in_place_enter_saves_the_setting() {
    for size in SIZES {
        let mut h = page(size, "seeded", "");
        h.sent();
        h.key(b"f");
        let s = h.shoot("skills-shelf-editing");
        assert!(s.contains("Enter saves · Esc keeps"), "{s}");
        h.type_text("/srv/new-shelf");
        h.key(b"\r");
        let cmds = h.sent();
        let body = cmds
            .iter()
            .find_map(|c| match c {
                Cmd::SaveRuntimeConfig { body, form_id } => {
                    assert!(form_id.is_some());
                    Some(body.0.clone())
                }
                _ => None,
            })
            .unwrap_or_else(|| panic!("no save sent: {cmds:?}"));
        assert_eq!(body, json!({"skills.shelf": "/srv/new-shelf"}));
    }
}

#[test]
fn esc_keeps_the_folder_and_sends_nothing() {
    let mut h = page((80, 24), "stored", "/srv/skills");
    h.sent();
    h.key(b"f");
    h.type_text("zzz");
    let s = h.esc();
    assert!(!s.contains("Enter saves"), "{s}");
    assert!(s.contains("Shelf folder: /srv/skills"), "{s}");
    assert!(
        !h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::SaveRuntimeConfig { .. })),
        "Esc must not save"
    );
}

#[test]
fn u_refreshes_the_curated_shelf() {
    let mut h = page((80, 24), "seeded", "");
    h.sent();
    let s = h.key(b"u");
    assert!(s.contains("Working..."), "{s}");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::Operator(OpCmd::ReseedSkills))),
        "u sends the reseed"
    );
}

#[test]
fn a_non_admin_sees_no_shelf_row() {
    let mut h = page((80, 24), "seeded", "");
    h.identity("alice", false);
    let s = h.text();
    assert!(!s.contains("Shelf folder:"), "{s}");
}

/// Live: save a folder, read it back from the gateway, clear it, refresh.
#[test]
#[ignore]
fn live_shelf_save_and_refresh() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let shelf = std::env::var("R8W4_SHELF_DIR").expect("R8W4_SHELF_DIR (an existing shelf folder)");
    let mut h = live((120, 40), Mount::Page(skills_mcp::screen), &url, &token);
    skills_mcp::refresh_for_tests(&h.store, &h.tx);
    h.until_text("Shelf folder:");
    h.key(b"f");
    h.type_text(&shelf);
    h.key(b"\r");
    h.until_text("Saved");
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(cfg["skills"]["shelf"]["value"], json!(shelf), "{cfg}");
    assert_eq!(cfg["skills"]["shelf"]["source"], json!("stored"));
    h.shoot("live-skills-shelf-saved");
    // Clear back to the gateway's own copy.
    h.key(b"f");
    for _ in 0..shelf.chars().count() + 2 {
        h.key(b"\x7f");
    }
    h.key(b"\r");
    h.until("cleared", |h, _| {
        h.store.runtime_config.with_untracked(|r| match r {
            Loadable::Ready(d) => d
                .skills_shelf
                .as_ref()
                .is_some_and(|s| s.source != "stored"),
            _ => false,
        })
    });
    let cfg = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_ne!(cfg["skills"]["shelf"]["source"], json!("stored"), "{cfg}");
    h.key(b"u");
    let s = h.until_text("Refreshed.");
    assert!(s.contains("Curated shelf"), "{s}");
    h.shoot("live-skills-shelf-refreshed");
}
