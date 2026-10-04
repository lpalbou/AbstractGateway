//! Skills & MCP (R7.2): snapshot tests per screen state (hermetic: the
//! store is seeded with the gateway's own payload shapes) and drive tests
//! per action against the live scratch gateway (ignored by default):
//!
//!   R7W1_URL=http://127.0.0.1:18785 R7W1_TOKEN=r7w1-admin-token-0000 \
//!   R7W1_SHOTS_DIR=<dir> cargo test --test r7w1_skills_mcp -- --ignored --test-threads 1

mod r7w1;

use abstractgateway_console::store::skills::{mcp_from_payload, skills_from_payload};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::skills_mcp;
use abstractgateway_console::worker::skills::SkCmd;
use abstractgateway_console::worker::Cmd;
use r7w1::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn skills_payload() -> Value {
    json!({"skills": [
        {"name": "field-guide", "description": "Answers questions about the field guide.", "origin": "curated",
         "source_label": "Shelf folder", "version": "2026.10.04", "editable": false, "archived": false,
         "trust_level": "unverified", "blocked": false, "reasons": ["unverified: no validation record for this tree hash — review before use"]},
        {"name": "field-notes", "description": "Keeps short field notes for a project, with a long description that has to wrap onto more lines at eighty columns.", "origin": "imported",
         "source_label": "Imported", "version": "1.4.0", "editable": true, "archived": false,
         "trust_level": "first_party", "blocked": false, "reasons": []},
        {"name": "old-notes", "description": "An older note-taking skill.", "origin": "archived",
         "source_label": "Imported (archived)", "version": "1.0.0", "editable": false, "archived": true}
    ], "warnings": ["#FALLBACK: hidden", "The shelf folder has no validations file."]})
}

fn mcp_payload() -> Value {
    json!({"servers": [
        {"name": "calc", "transport": "stdio", "command": "python3", "args": ["-u", "/tmp/fake_stdio.py"],
         "description": "Adds and echoes (test stub).", "archived": false, "enabled_for_agents": false,
         "last_test": {"ok": true, "at": "2026-10-04T02:00:00Z", "message": "Connected to fake-stdio 0.3 · 2 tools.",
                       "tools": [{"name": "add", "description": "Add two numbers"}, {"name": "echo", "description": "Echo text"}]},
         "agents_status": "Not offered to agents"},
        {"name": "docs", "transport": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": {"fingerprint": "0123456789abcdef"}},
         "description": "", "archived": false, "enabled_for_agents": false, "last_test": null, "agents_status": "Not offered: test the connection first"},
        {"name": "legacy-docs", "transport": "http", "url": "http://127.0.0.1:9/old", "description": "An old docs server.",
         "archived": true, "enabled_for_agents": false, "last_test": null, "agents_status": "Not offered: archived"}
    ], "agents_note": "No server is offered to agents yet: turn on Enabled for agents for a tested server to offer its tools.", "warnings": []})
}

fn page(size: (i32, i32)) -> r7w1::Harness {
    let mut h = harness(size, Mount::Page(skills_mcp::screen));
    h.admin();
    h.store.skills.skills.set(Loadable::Ready(
        skills_from_payload(&skills_payload()).unwrap(),
    ));
    h.store
        .skills
        .mcp
        .set(Loadable::Ready(mcp_from_payload(&mcp_payload()).unwrap()));
    h.turns(3);
    h
}

#[test]
fn skills_tab_shows_the_shelf_with_the_web_words() {
    for size in SIZES {
        let mut h = page(size);
        let s = h.shoot("skills");
        assert!(s.contains("Skills & MCP"), "{s}");
        assert!(s.contains("[Skills]"), "{s}");
        assert!(s.contains("curated ones ship with the"), "{s}");
        assert!(s.contains("[ ] Show archived"), "{s}");
        assert!(s.contains("field-guide") && s.contains("Unverified"), "{s}");
        assert!(s.contains("First party"), "{s}");
        assert!(
            s.contains("The shelf folder has no validations file."),
            "{s}"
        );
        assert!(!s.contains("#FALLBACK"), "{s}");
        // The long description wraps instead of being cut.
        assert!(!s.contains('…'), "a cell was cut:\n{s}");
        if size.0 >= 100 {
            assert!(
                s.contains("Version") && s.contains("Source") && s.contains("Shelf folder"),
                "{s}"
            );
        }
        h.assert_fits();
    }
}

#[test]
fn enter_expands_a_skill_row_with_its_actions() {
    let mut h = page((80, 24));
    h.key(b"\x1b[B"); // field-notes
    let s = h.key(b"\r");
    assert!(s.contains("Version: 1.4.0 · Source: Imported"), "{s}");
    assert!(s.contains("Actions: v View · x Export · d Archive"), "{s}");
    h.shoot("skills-expanded");
}

#[test]
fn search_filters_and_says_no_match() {
    let mut h = page((80, 24));
    h.store.skills.query.set("zzz".into());
    let s = h.text();
    assert!(s.contains("No skill matches this search."), "{s}");
    h.store.skills.query.set("notes".into());
    let s = h.text();
    assert!(
        s.contains("field-notes") && !s.contains("field-guide"),
        "{s}"
    );
}

#[test]
fn archive_asks_inline_then_sends_the_web_route() {
    let mut h = page((80, 24));
    h.sent();
    h.key(b"\x1b[B"); // field-notes (imported)
    let s = h.key(b"d");
    assert!(
        s.contains("Archive field-notes?") && s.contains("[y] Archive") && s.contains("[n] Keep"),
        "{s}"
    );
    assert!(h.sent().is_empty(), "nothing before y");
    h.key(b"y");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::Skills(SkCmd::SetSkillArchived { name, archive: true, .. }) if name == "field-notes")),
        "{sent:?}"
    );
}

#[test]
fn a_curated_skill_cannot_be_archived_and_says_why() {
    let mut h = page((80, 24));
    h.sent();
    let s = h.key(b"d");
    assert!(
        s.contains("Curated skills are read-only; duplicate to edit."),
        "{s}"
    );
    assert!(h.sent().is_empty());
}

#[test]
fn mcp_tab_shows_servers_status_and_the_agents_switch() {
    for size in SIZES {
        let mut h = page(size);
        h.key(b"\x1b[C");
        let s = h.shoot("mcp");
        assert!(s.contains("[MCP servers]"), "{s}");
        assert!(s.contains("No server is offered to agents yet"), "{s}");
        assert!(s.contains("Test runs the real handshake"), "{s}");
        assert!(s.contains("calc") && s.contains("OK · 2 tools"), "{s}");
        assert!(s.contains("[ ] Enabled for agents"), "{s}");
        assert!(s.contains("Not tested"), "{s}");
        assert!(!s.contains("legacy-docs"), "archived hidden:\n{s}");
        h.key(b"h");
        let s = h.text();
        assert!(s.contains("legacy-docs") && s.contains("Archived"), "{s}");
        assert!(!s.contains('…'), "a cell was cut:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn turning_agents_on_asks_first_and_a_blocked_switch_says_why() {
    let mut h = page((120, 40));
    h.key(b"\x1b[C");
    h.sent();
    let s = h.key(b" ");
    assert!(
        s.contains("Offer its 2 tools to your agents?") && s.contains("[y] Turn on"),
        "{s}"
    );
    h.key(b"n");
    assert!(h.sent().is_empty(), "Keep sends nothing");
    h.key(b" ");
    h.key(b"y");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::Skills(SkCmd::SetMcpAgents { name, enabled: true }) if name == "calc")
    ));
    h.key(b"\x1b[B"); // docs: not tested
    let s = h.key(b" ");
    assert!(
        s.contains(
            "docs: Test the connection first: agents get the tools a successful test lists."
        ),
        "{s}"
    );
    assert!(h.sent().is_empty());
}

#[test]
fn add_server_overlay_has_the_web_fields_and_esc_closes_it() {
    for size in SIZES {
        let mut h = page(size);
        h.key(b"\x1b[C");
        let s = h.key(b"a");
        assert!(s.contains("Add MCP server"), "{s}");
        assert!(s.contains("A short name for this server"), "{s}");
        assert!(s.contains("[Command]") && s.contains("URL"), "{s}");
        assert!(s.contains("Save and test the server first"), "{s}");
        h.shoot("mcp-add");
        let s = h.key(b"\x1b");
        assert!(!s.contains("Add MCP server"), "Esc closes:\n{s}");
        h.key(b"a");
        let s = h.wheel_down(10);
        assert!(s.contains("Test connection") && s.contains("Save"), "{s}");
        h.shoot("mcp-add-scrolled");
        let s = h.key(b"\x1b");
        assert!(!s.contains("Add MCP server"), "Esc closes:\n{s}");
    }
}

#[test]
fn the_skill_overlay_shows_read_only_reason_and_duplicate() {
    let mut h = page((120, 40));
    h.key(b"v");
    h.store.skills.detail.set(Loadable::Ready(
        abstractgateway_console::store::skills::skill_detail_from_payload(&json!({
            "name": "field-guide", "origin": "curated", "archived": false, "editable": false,
            "read_only_reason": "Curated skills are read-only; duplicate to edit.",
            "skill_md": "---\nname: field-guide\n---\n# Field guide\n", "frontmatter": {"description": "Answers questions."},
            "version": "2026.10.04", "files": [{"path": "SKILL.md", "size": 40}]
        }))
        .unwrap(),
    ));
    let s = h.shoot("skill-overlay");
    assert!(s.contains("Skill — field-guide"), "{s}");
    assert!(
        s.contains("Curated skills are read-only; duplicate to edit."),
        "{s}"
    );
    assert!(s.contains("Duplicate to edit"), "{s}");
    assert!(s.contains("SKILL.md 40 B"), "{s}");
}

// ---------------------------------------------------------------- live

fn select_skill(h: &mut r7w1::Harness, name: &str) {
    let idx = h.store.skills.skills.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.rows.iter().position(|r| r.name == name))
    });
    h.store.skills.skill_sel.set(idx.expect(name));
    h.turns(2);
}

fn live_page(size: (i32, i32)) -> Option<(r7w1::Harness, String, String)> {
    let (url, token) = live_env()?;
    let mut h = live(size, Mount::Page(skills_mcp::screen), &url, &token);
    skills_mcp::refresh_for_tests(&h.store, &h.tx);
    h.until_text("field-guide");
    Some((h, url, token))
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_skills_archive_unarchive_export_import() {
    let Some((mut h, url, token)) = live_page((120, 40)) else {
        return;
    };
    // Start state: old-notes archived (idempotent re-runs).
    gw(
        "POST",
        &url,
        &token,
        "/admin/skills/old-notes/archive",
        Some(json!({})),
    );
    skills_mcp::refresh_for_tests(&h.store, &h.tx);
    h.turns(5);
    h.shoot("live-skills");
    // Show archived lists old-notes; Unarchive it (no confirm), then archive again (inline confirm).
    h.key(b"h");
    h.until_text("old-notes");
    select_skill(&mut h, "old-notes");
    h.key(b"d");
    h.until_text("Unarchived old-notes: it is back on the shelf.");
    select_skill(&mut h, "old-notes");
    let v = gw("GET", &url, &token, "/skills", None);
    assert!(
        v["skills"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "old-notes"),
        "{v}"
    );
    h.key(b"d");
    h.until_text("Archive old-notes?");
    h.key(b"y");
    h.until_text("Archived old-notes: runs no longer see it.");
    let v = gw("GET", &url, &token, "/skills", None);
    assert!(
        !v["skills"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "old-notes"),
        "{v}"
    );
    // Export field-notes to a scratch dir, then import that zip back under a new name is refused (exists).
    let dir = std::env::temp_dir().join(format!("r7w1-export-{}", std::process::id()));
    h.tx.send(Cmd::Skills(SkCmd::ExportSkill {
        name: "field-notes".into(),
        dir: dir.clone(),
    }))
    .unwrap();
    h.until_text("Exported field-notes to");
    let zip = dir.join("field-notes.zip");
    assert!(std::fs::metadata(&zip).unwrap().len() > 0);
    h.tx.send(Cmd::Skills(SkCmd::ImportSkill {
        path: zip.display().to_string(),
        include_archived: true,
    }))
    .unwrap();
    h.until_text("Not imported:");
    h.shoot("live-skills-import-refused");
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_mcp_test_enable_disable_archive() {
    let Some((mut h, url, token)) = live_page((120, 40)) else {
        return;
    };
    gw(
        "POST",
        &url,
        &token,
        "/admin/mcp/servers/calc/agents",
        Some(json!({"enabled": false})),
    );
    skills_mcp::refresh_for_tests(&h.store, &h.tx);
    h.key(b"\x1b[C");
    h.until_text("calc");
    h.key(b"t");
    h.until_text("calc: Connected to fake-stdio 0.3 · 2 tools.");
    h.key(b" ");
    h.until_text("Offer its 2 tools to your agents?");
    h.key(b"y");
    h.until_text("calc: Offered to agents");
    let v = gw("GET", &url, &token, "/mcp/servers", None);
    let calc = v["servers"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["name"] == "calc")
        .unwrap()
        .clone();
    assert_eq!(calc["enabled_for_agents"], true, "{calc}");
    h.shoot("live-mcp-enabled");
    h.key(b" ");
    h.until_text("calc: Not offered to agents");
    let v = gw("GET", &url, &token, "/mcp/servers", None);
    let calc = v["servers"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["name"] == "calc")
        .unwrap()
        .clone();
    assert_eq!(calc["enabled_for_agents"], false, "{calc}");
}
