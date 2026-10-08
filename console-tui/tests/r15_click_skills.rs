//! R15 (DESIGN-TUI.md §3.5): a synthesized mouse click for EVERY Skills &
//! MCP control — the [Skills] [MCP servers] segments, each skill row's View
//! / Export / Archive / Unarchive, Import .zip / Import folder, the shelf
//! folder + Refresh curated shelf, each server row's Edit / Test / Archive /
//! Unarchive, the Enabled for agents toggle (its [Turn on] [Cancel]
//! question answered by mouse), Add server, and the editors' footer
//! buttons — through the real input pipeline. The meta-test enumerates
//! `skill_actions` / `mcp_actions` / `panel_actions` over the fixture:
//! an action without a click test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_skills.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::skills::{
    mcp_from_payload, skill_detail_from_payload, skills_from_payload,
};
use abstractgateway_console::store::{Loadable, RuntimeConfigData};
use abstractgateway_console::ui::skills_mcp;
use abstractgateway_console::worker::skills::SkCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn skills_payload() -> Value {
    json!({"skills": [
        {"name": "field-guide", "description": "Answers questions about the field guide.", "origin": "curated",
         "source_label": "Curated registry", "version": "2026.10.04", "editable": false, "archived": false,
         "trust_level": "unverified", "blocked": false, "reasons": ["unverified: no validation record"]},
        {"name": "field-notes", "description": "Keeps short field notes.", "origin": "imported",
         "source_label": "Imported", "version": "1.4.0", "editable": true, "archived": false,
         "trust_level": "first_party", "blocked": false, "reasons": []},
        {"name": "old-notes", "description": "An older note-taking skill.", "origin": "archived",
         "source_label": "Imported (archived)", "version": "1.0.0", "editable": false, "archived": true}
    ], "warnings": []})
}

fn mcp_payload() -> Value {
    json!({"servers": [
        {"name": "calc", "transport": "stdio", "command": "python3", "args": ["-u", "/tmp/fake.py"],
         "description": "Adds and echoes.", "archived": false, "enabled_for_agents": false,
         "last_test": {"ok": true, "at": "2026-10-04T02:00:00Z", "message": "Connected · 2 tools.",
                       "tools": [{"name": "add", "description": "Add"}, {"name": "echo", "description": "Echo"}]},
         "agents_status": "Not offered to agents"},
        {"name": "docs", "transport": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": {"fingerprint": "0123456789abcdef"}},
         "description": "", "archived": false, "enabled_for_agents": false, "last_test": null, "agents_status": "Not offered"},
        {"name": "legacy", "transport": "http", "url": "http://127.0.0.1:9/old", "description": "",
         "archived": true, "enabled_for_agents": false, "last_test": null, "agents_status": "Not offered: archived"}
    ], "agents_note": "", "warnings": []})
}

fn runtime_config() -> Value {
    json!({"writable": true, "skills": {"shelf": {"key": "skills.shelf", "label": "Skills shelf", "value": "", "source": "seeded",
        "resolved": "/data/skills", "available": true, "reason": "", "default_path": "/data/skills", "bundled_version": "2026.10.01"}}})
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_skills.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn page() -> r8w4::Harness {
    let mut h = harness((140, 44), Mount::Page(skills_mcp::screen));
    h.admin();
    h.store.skills.skills.set(Loadable::Ready(
        skills_from_payload(&skills_payload()).unwrap(),
    ));
    h.store
        .skills
        .mcp
        .set(Loadable::Ready(mcp_from_payload(&mcp_payload()).unwrap()));
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(
            &runtime_config(),
        )));
    h.turns(3);
    h.sent();
    h
}

/// Click a head segment (the head row's LAST occurrence of `label`).
fn click_segment(h: &mut r8w4::Harness, label: &str) -> String {
    let s = h.turns(1);
    let line = s.lines().next().unwrap_or_default().to_string();
    let b = line.rfind(label).unwrap_or_else(|| panic!("{label}:\n{s}"));
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};1M\x1b[<0;{x};1m").as_bytes())
}

fn mcp_page() -> r8w4::Harness {
    let mut h = page();
    click_segment(&mut h, "MCP servers");
    h.sent();
    h
}

fn sk_cmds(h: &mut r8w4::Harness) -> Vec<SkCmd> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Skills(s) => Some(s),
            _ => None,
        })
        .collect()
}

/// Click `needle` (searched right of the name) on `name`'s row.
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(&format!(" {name} ")))
        .unwrap_or_else(|| panic!("{name} row:\n{screen}"));
    let byte = line
        .rfind(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on {name}'s row:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the `label` button of an open dialog (its button row holds
/// `label` and `other`).
fn click_pair(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] button row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    for r in &skills_from_payload(&skills_payload()).unwrap().rows {
        for a in skills_mcp::skill_actions(r, true) {
            out.insert((r.name.clone(), a.id));
        }
    }
    for r in &mcp_from_payload(&mcp_payload()).unwrap().rows {
        for a in skills_mcp::mcp_actions(r, true) {
            out.insert((r.name.clone(), a.id));
        }
    }
    for tab in [0, 1] {
        for a in skills_mcp::panel_actions(tab, true) {
            out.insert((format!("panel{tab}"), a.id));
        }
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("field-guide", "view"),
        ("field-guide", "export"),
        ("field-notes", "view"),
        ("field-notes", "export"),
        ("field-notes", "archive"),
        ("old-notes", "view"),
        ("old-notes", "unarchive"),
        ("calc", "edit"),
        ("calc", "test"),
        ("calc", "archive"),
        ("docs", "edit"),
        ("docs", "test"),
        ("docs", "archive"),
        ("legacy", "unarchive"),
        ("panel0", "import_zip"),
        ("panel0", "import_folder"),
        ("panel0", "reseed"),
        ("panel1", "add"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_skills_mcp_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Skills & MCP actions without a click test: {missing:?}"
    );
}

#[test]
fn the_page_has_the_web_words() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "Skills & MCP",
        "Skills agents can load, and MCP tool servers",
        " Skills ",
        "MCP servers",
        "curated ones ship with the gateway",
        "Search by name or description",
        "●─ Show archived",
        " Import .zip ",
        "Import folder",
        "Shelf folder",
        "Refresh curated shelf",
        "field-guide",
        "Unverified",
        "First party",
        " View ",
        " Export ",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    h.assert_fits();
}

#[test]
fn clicks_on_the_skill_rows() {
    let mut h = page();
    let s = click_row(&mut h, "field-guide", "View");
    assert!(s.contains("Skill — field-guide"), "{s}");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::OpenSkill { name } if name == "field-guide")));
    let mut h = page();
    click_row(&mut h, "field-guide", "Export");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::ExportSkill { name, .. } if name == "field-guide")));
    let mut h = page();
    click_row(&mut h, "field-notes", "View");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::OpenSkill { name } if name == "field-notes")));
    let mut h = page();
    click_row(&mut h, "field-notes", "Export");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::ExportSkill { name, .. } if name == "field-notes")));
    // Archive: at once, as on the web.
    let mut h = page();
    click_row(&mut h, "field-notes", "Archive");
    assert!(sk_cmds(&mut h).iter().any(|c| matches!(c, SkCmd::SetSkillArchived { name, archive: true, .. } if name == "field-notes")));
    // The archived skill (Show archived lists it).
    let mut h = page();
    h.click_text("●─ Show archived");
    assert!(sk_cmds(&mut h).iter().any(|c| matches!(
        c,
        SkCmd::LoadSkills {
            include_archived: true
        }
    )));
    click_row(&mut h, "old-notes", "Unarchive");
    assert!(sk_cmds(&mut h).iter().any(
        |c| matches!(c, SkCmd::SetSkillArchived { name, archive: false, .. } if name == "old-notes")
    ));
    let mut h = page();
    click_row(&mut h, "old-notes", "View");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::OpenSkill { name } if name == "old-notes")));
}

#[test]
fn the_skill_editor_saves_and_asks_before_dropping_an_edit() {
    let mut h = page();
    click_row(&mut h, "field-notes", "View");
    h.store.skills.detail.set(Loadable::Ready(
        skill_detail_from_payload(&json!({
            "name": "field-notes", "origin": "imported", "archived": false, "editable": true,
            "skill_md": "---\nname: field-notes\n---\n# Notes\n", "frontmatter": {"description": "Keeps notes."},
            "version": "1.4.0", "files": [{"path": "SKILL.md", "size": 40}]
        }))
        .unwrap(),
    ));
    let s = h.turns(3);
    assert!(s.contains("SKILL.md 40 B") && s.contains(" Save "), "{s}");
    h.sent();
    // Edit the version, Close → the question; Keep editing → Save.
    h.click_text("▐1.4.0");
    h.type_text("9");
    let s = click_pair(&mut h, "Close", "Save");
    assert!(s.contains("Discard changes?"), "{s}");
    click_pair(&mut h, "Keep editing", "Discard");
    click_pair(&mut h, "Save", "Close");
    assert!(sk_cmds(&mut h).iter().any(|c| matches!(c, SkCmd::SaveSkill { name, body, .. } if name == "field-notes" && body.0.get("version").is_some())));
}

#[test]
fn a_curated_skill_offers_duplicate_and_an_archived_one_unarchive() {
    let mut h = page();
    click_row(&mut h, "field-guide", "View");
    h.store.skills.detail.set(Loadable::Ready(
        skill_detail_from_payload(&json!({
            "name": "field-guide", "origin": "curated", "archived": false, "editable": false,
            "read_only_reason": "Curated skills are read-only; duplicate to edit.",
            "skill_md": "# Field guide\n", "frontmatter": {}, "version": "1", "files": []
        }))
        .unwrap(),
    ));
    h.turns(3);
    h.sent();
    click_pair(&mut h, "Duplicate to edit", "Close");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::DuplicateSkill { name, .. } if name == "field-guide")));
    let mut h = page();
    h.click_text("●─ Show archived");
    click_row(&mut h, "old-notes", "View");
    h.store.skills.detail.set(Loadable::Ready(
        skill_detail_from_payload(&json!({
            "name": "old-notes", "origin": "archived", "archived": true, "editable": false,
            "skill_md": "# Old\n", "frontmatter": {}, "version": "1", "files": []
        }))
        .unwrap(),
    ));
    h.turns(3);
    h.sent();
    click_pair(&mut h, "Unarchive", "Close");
    assert!(sk_cmds(&mut h).iter().any(|c| matches!(c, SkCmd::SetSkillArchived { name, archive: false, reopen: true, .. } if name == "old-notes")));
}

#[test]
fn imports_and_the_shelf_row() {
    let mut h = page();
    let s = h.click_text(" Import .zip ");
    assert!(
        s.contains("Import a skill from a .zip of its folder"),
        "{s}"
    );
    h.type_text("/tmp/notes.zip");
    click_pair(&mut h, "Import", "Close");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::ImportSkill { path, .. } if path == "/tmp/notes.zip")));
    let mut h = page();
    let s = h.click_text("Import folder");
    assert!(
        s.contains("Import a skill folder (it holds SKILL.md)"),
        "{s}"
    );
    h.type_text("/tmp/notes/");
    click_pair(&mut h, "Import", "Close");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::ImportSkill { path, .. } if path == "/tmp/notes/")));
    // Refresh curated shelf.
    let mut h = page();
    h.click_text("Refresh curated shelf");
    assert!(h.sent().iter().any(|c| matches!(
        c,
        Cmd::Operator(abstractgateway_console::worker::operator::OpCmd::ReseedSkills)
    )));
    // The shelf folder: type, Enter → the runtime-config write.
    let mut h = page();
    h.click_right_of("Shelf folder", 16);
    h.type_text("/srv/skills\r");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveRuntimeConfig { body, .. } if body.0["skills.shelf"] == "/srv/skills")));
}

#[test]
fn clicks_on_the_server_rows() {
    let mut h = mcp_page();
    let s = click_row(&mut h, "calc", "Edit");
    assert!(s.contains("MCP server — calc"), "{s}");
    let mut h = mcp_page();
    click_row(&mut h, "calc", "Test");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::TestMcp { name } if name == "calc")));
    let mut h = mcp_page();
    click_row(&mut h, "calc", "Archive");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::SetMcpArchived { name, archive: true } if name == "calc")));
    let mut h = mcp_page();
    let s = click_row(&mut h, "docs", "Edit");
    assert!(
        s.contains("MCP server — docs") && s.contains("fingerprint"),
        "{s}"
    );
    let mut h = mcp_page();
    click_row(&mut h, "docs", "Test");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::TestMcp { name } if name == "docs")));
    let mut h = mcp_page();
    click_row(&mut h, "docs", "Archive");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::SetMcpArchived { name, archive: true } if name == "docs")));
    let mut h = mcp_page();
    h.click_text("●─ Show archived");
    click_row(&mut h, "legacy", "Unarchive");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::SetMcpArchived { name, archive: false } if name == "legacy")));
}

#[test]
fn enabled_for_agents_asks_turn_on_by_mouse_and_a_blocked_one_says_why() {
    let mut h = mcp_page();
    let s = click_row(&mut h, "calc", "●─");
    assert!(s.contains("Offer its 2 tools to your agents?"), "{s}");
    click_pair(&mut h, "Cancel", "Turn on");
    assert!(sk_cmds(&mut h).is_empty(), "Cancel sends nothing");
    click_row(&mut h, "calc", "●─");
    click_pair(&mut h, "Turn on", "Cancel");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::SetMcpAgents { name, enabled: true } if name == "calc")));
    // A refused toggle in an unselected row: the first click selects the
    // row, the second says the reason (lead: w/toggle.rs, COORD R15-A).
    let mut h = mcp_page();
    click_row(&mut h, "docs", "●─");
    let s = click_row(&mut h, "docs", "●─");
    let said = h.store.notice.get_untracked().unwrap_or_default();
    assert!(said.contains("Test the connection first"), "{said:?}\n{s}");
    assert!(sk_cmds(&mut h).is_empty());
}

#[test]
fn add_server_form_tests_saves_and_switches_how_to_reach() {
    let mut h = mcp_page();
    let s = h.click_text("Add server");
    assert!(
        s.contains("Add MCP server") && s.contains("A short name for this server"),
        "{s}"
    );
    h.type_text("echo");
    h.click_text("npx");
    h.type_text("npx");
    click_pair(&mut h, "Test connection", "Save");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::TestMcpForm { body } if body.0["command"] == "npx")));
    click_pair(&mut h, "Save", "Close");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::SaveMcp { editing: None, body } if body.0["name"] == "echo")));
    // URL segment: the URL pane with Add header.
    let s = h.click_after("How to reach it", "URL");
    assert!(
        s.contains("https://example.com/mcp") && s.contains("Add header"),
        "{s}"
    );
    let s = h.click_text("Add header");
    assert!(s.contains("Name, e.g. Authorization"), "{s}");
    // Close with edits → the question.
    let s = click_pair(&mut h, "Close", "Save");
    assert!(s.contains("Discard changes?"), "{s}");
}

#[test]
fn the_segments_switch_panels_and_the_keyboard_reaches_actions() {
    let mut h = page();
    let s = click_segment(&mut h, "MCP servers");
    assert_eq!(h.store.skills.tab.get_untracked(), 1, "{s}");
    assert!(s.contains("Test runs the real handshake"), "{s}");
    let s = click_segment(&mut h, "Skills");
    assert_eq!(h.store.skills.tab.get_untracked(), 0, "{s}");
    // Keyboard: ↓ to field-notes, x exports it.
    let mut h = page();
    h.key(b"\x1b[B");
    h.key(b"x");
    assert!(sk_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, SkCmd::ExportSkill { name, .. } if name == "field-notes")));
}

#[test]
fn hovering_import_zip_says_the_web_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find("Import .zip").map(|c| (i, l[..c].chars().count())))
        .expect("Import .zip");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(
        s.contains("Import a skill from a .zip of its folder  (i)"),
        "{s}"
    );
}

#[test]
fn a_modal_survives_the_reload_it_causes() {
    let mut h = mcp_page();
    click_row(&mut h, "calc", "Edit");
    h.store
        .skills
        .mcp
        .set(Loadable::Ready(mcp_from_payload(&mcp_payload()).unwrap()));
    let s = h.turns(3);
    assert!(s.contains("MCP server — calc"), "{s}");
}

#[test]
fn fits_at_80x24() {
    for tab in [0usize, 1] {
        let mut h = harness((80, 24), Mount::Page(skills_mcp::screen));
        h.admin();
        h.store.skills.tab.set(tab);
        h.store.skills.skills.set(Loadable::Ready(
            skills_from_payload(&skills_payload()).unwrap(),
        ));
        h.store
            .skills
            .mcp
            .set(Loadable::Ready(mcp_from_payload(&mcp_payload()).unwrap()));
        let s = h.turns(3);
        assert!(
            s.contains(if tab == 0 { "field-guide" } else { "calc" }),
            "{s}"
        );
        // The panel buttons wrap onto their own row rather than leave the page.
        assert!(
            s.contains(if tab == 0 {
                "Import folder"
            } else {
                "Add server"
            }),
            "{s}"
        );
        h.assert_fits();
    }
}

#[test]
fn the_words_are_the_webs() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(skills_mcp::TABS[0], w("tab_skills"));
    assert_eq!(skills_mcp::TABS[1], w("tab_mcp"));
    assert_eq!(skills_mcp::SEARCH_PLACEHOLDER, w("search_placeholder"));
    assert_eq!(skills_mcp::IMPORT_ZIP, w("import_zip"));
    assert_eq!(skills_mcp::IMPORT_ZIP_TIP, w("import_zip_tip"));
    assert_eq!(skills_mcp::IMPORT_FOLDER, w("import_folder"));
    assert_eq!(skills_mcp::IMPORT_FOLDER_TIP, w("import_folder_tip"));
    assert_eq!(skills_mcp::ADD_SERVER, w("add_server"));
    assert_eq!(skills_mcp::REFRESH_SHELF, w("refresh_shelf"));
    assert_eq!(skills_mcp::SHELF_LABEL, w("shelf_label"));
    use abstractgateway_console::store::skills as st;
    assert_eq!(st::SKILLS_PURPOSE, w("skills_purpose"));
    assert_eq!(st::MCP_PURPOSE, w("mcp_purpose"));
    assert_eq!(st::AGENTS_LABEL, w("agents_label"));
    let labels: Vec<String> = ["view", "export", "archive", "unarchive"]
        .iter()
        .map(|k| w(&format!("skill_{k}")))
        .collect();
    let rows = skills_from_payload(&skills_payload()).unwrap().rows;
    for r in &rows {
        for a in skills_mcp::skill_actions(r, true) {
            assert!(labels.contains(&a.label), "{}: {}", r.name, a.label);
        }
    }
    let labels: Vec<String> = ["edit", "test", "archive", "unarchive"]
        .iter()
        .map(|k| w(&format!("mcp_{k}")))
        .collect();
    for r in &mcp_from_payload(&mcp_payload()).unwrap().rows {
        for a in skills_mcp::mcp_actions(r, true) {
            assert!(labels.contains(&a.label), "{}: {}", r.name, a.label);
        }
    }
    let calc = &mcp_from_payload(&mcp_payload()).unwrap().rows[0];
    assert_eq!(
        calc.agents_confirm_sentence(),
        w("agents_confirm").replace("{k}", "2").replace("{s}", "s")
    );
    let mut h = page();
    let s = h.turns(1);
    for k in [
        "col_name",
        "col_what",
        "col_version",
        "col_trust",
        "col_source",
    ] {
        assert!(s.contains(&w(k)), "{k}:\n{s}");
    }
}
