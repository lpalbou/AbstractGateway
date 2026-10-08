//! R15 (DESIGN-TUI.md §3.4): a synthesized mouse click for EVERY Workflows
//! control — each row's glyph actions (Export, Open in AbstractFlow,
//! Archive / Unarchive, Edit description) on bundle AND older-version
//! rows, the Available to users toggle, the head's ↻ and Import .flow, the
//! three switches, the default-workflow pickers, Streamed replies and the
//! broken section's Archive — through the real input pipeline. The
//! meta-test enumerates `workflows::row_actions` / `broken_actions` /
//! `head_actions` over the fixture: an action without a click test is RED.
//! The words are the web's (`tests/fixtures/r15_web_wording_workflows.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::workflows_page::{
    defaults_from_payload, workflows_from_payload,
};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, workflows};
use abstractgateway_console::worker::workflows::WfCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    workflows::view(cx, ctx, &t)
}

fn version(owner: &str, id: &str, name: &str, v: &str, source: &str, extra: Value) -> Value {
    let mut it = json!({"bundle_id": id, "bundle_version": v, "owner": {"kind": owner}, "is_draft": false,
        "archived": false, "available": true, "source": source, "description": format!("{name} does its job."),
        "default_entrypoint": "f1", "created_at": "2026-10-01T10:00:00Z", "version_channel": "",
        "entrypoints": [{"flow_id": "f1", "name": name, "description": "", "interfaces": ["abstractcode.agent.v1"]}],
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
        version("gateway", "team-digest", "Team digest", "2.0.0", "imported", json!({"available": false})),
        version("user", "note-taker", "Note taker", "1.2.0", "imported", json!({})),
        version("user", "note-taker", "Note taker", "1.1.0", "imported", json!({})),
        version("user", "draft-helper", "Draft helper", "0.1.0", "published", json!({"archived": true})),
    ], "skipped": [
        {"bundle_id": "old-flow", "bundle_version": "0.1.0", "reason": "needs abstractruntime >= 0.9", "path": "/x/old-flow@0.1.0.flow", "can_archive": true, "archived": false},
        {"bundle_id": "old-flow", "bundle_version": "0.2.0", "reason": "needs abstractruntime >= 0.9", "path": "/x/old-flow@0.2.0.flow", "can_archive": true, "archived": false}
    ]})
}

fn defaults() -> Value {
    json!({"writable": true, "agents": {"default_workflow": {
        "abstractcode.agent.v1": {"label": "AbstractCode — chat agent", "help": "The agent AbstractCode runs.", "group": "apps",
            "state": "builtin", "value": "basic-agent@0.0.5:f1", "source": "default", "default": "basic-agent@0.0.5:f1",
            "reason": null, "resolved": {"name": "Basic agent", "bundle_version": "0.0.5"},
            "eligible": [{"value": "basic-agent@0.0.5:f1", "name": "Basic agent", "bundle_version": "0.0.5"},
                         {"value": "note-taker@1.2.0:f1", "name": "Note taker", "bundle_version": "1.2.0"}]},
        "batch.map.v1": {"label": "Batch map-reduce", "help": "Runs over many inputs.", "group": "other", "state": "none",
            "value": "", "source": "default", "default": "", "reason": null, "eligible": []}
    }, "streaming_default": {"value": true}}})
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_workflows.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn page_sized(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .wf
        .data
        .set(Loadable::Ready(workflows_from_payload(&bundles()).unwrap()));
    h.store
        .wf
        .defaults
        .set(Loadable::Ready(defaults_from_payload(&defaults()).unwrap()));
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_sized((140, 50))
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

/// Click `needle` on the row whose line starts with `name` (searched in
/// the Actions cell: the last occurrence on the line).
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(&format!(" {name} ")) || l.starts_with(&format!("  {name} ")))
        .unwrap_or_else(|| panic!("{name} row:\n{screen}"));
    let byte = line
        .rfind(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on {name}'s row:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click `needle` on the `nth` (0-based) line holding `anchor`.
fn click_on_line(h: &mut r8w4::Harness, anchor: &str, nth: usize, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(anchor))
        .nth(nth)
        .unwrap_or_else(|| panic!("{anchor:?} #{nth}:\n{screen}"));
    let byte = line
        .rfind(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on the {anchor:?} line:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the `label` button of an open dialog (its button row holds
/// `label` and `other`).
fn click_confirm(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
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
    let d = workflows_from_payload(&bundles()).unwrap();
    let mut out = BTreeSet::new();
    for r in &d.rows {
        for a in workflows::row_actions(r, r.latest, false, true) {
            out.insert((r.bundle_id.clone(), a.id));
        }
        for v in 0..r.versions.len() {
            if v != r.latest {
                for a in workflows::row_actions(r, v, true, true) {
                    out.insert((format!("{}@{}", r.bundle_id, r.versions[v].version), a.id));
                }
            }
        }
    }
    for b in &d.broken {
        for a in workflows::broken_actions(b) {
            out.insert((b.bundle_id.clone(), a.id));
        }
    }
    for a in workflows::head_actions() {
        out.insert(("head".into(), a.id));
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("basic-agent", "export"),
        ("basic-agent", "open"),
        ("basic-agent", "archive"),
        ("basic-agent", "edit"),
        ("team-digest", "export"),
        ("team-digest", "open"),
        ("team-digest", "archive"),
        ("team-digest", "edit"),
        ("note-taker", "export"),
        ("note-taker", "open"),
        ("note-taker", "archive"),
        ("note-taker", "edit"),
        ("note-taker@1.1.0", "export"),
        ("note-taker@1.1.0", "open"),
        ("note-taker@1.1.0", "archive"),
        ("draft-helper", "export"),
        ("draft-helper", "open"),
        ("draft-helper", "unarchive"),
        ("draft-helper", "edit"),
        ("old-flow", "archive_broken"),
        ("head", "reload"),
        ("head", "import"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_workflow_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Workflows actions without a click test: {missing:?}"
    );
}

#[test]
fn the_page_is_one_page_with_the_web_words() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "Workflows",
        "Bundles, versions, import and export",
        "Workflows are the programs your apps and automations run.",
        "Search by name, description or id",
        "●─ Drafts",
        "●─ Older versions",
        "●─ Show archived",
        " ↻ ",
        "Import .flow",
        "Shared with everyone",
        "Mine",
        "Available to users",
        "Default workflow per app",
        "Gateway default: Basic agent 0.0.5",
        "Other workflow types",
        "Settings",
        "━● Streamed replies",
        "⚠ Broken workflows",
        "Archive 2",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    h.assert_fits();
}

#[test]
fn clicks_on_a_bundle_rows_glyphs() {
    // Export.
    let mut h = page();
    click_row(&mut h, "Note taker", "⤓");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::Export { bundle_id, version, .. } if bundle_id == "note-taker" && version == "1.2.0")));
    // Open in AbstractFlow → the Apps lane's signed link.
    let mut h = page();
    click_row(&mut h, "Note taker", "⇗");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::AppAct { app_id, path: Some(p), .. } if app_id == "flow" && p.contains("bundle=note-taker"))));
    // Archive → the web's confirmation, answered by mouse.
    let mut h = page();
    let s = click_row(&mut h, "Note taker", "⊟");
    assert!(
        s.contains("Archive Note taker? It disappears from lists"),
        "{s}"
    );
    assert!(wf_cmds(&mut h).is_empty(), "nothing before the confirm");
    click_confirm(&mut h, "Archive", "Cancel");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::Archive { bundle_id, version, .. } if bundle_id == "note-taker" && version.is_empty())));
    // Edit description → the form; Save sends the PATCH.
    let mut h = page();
    let s = click_row(&mut h, "Note taker", "✎");
    assert!(s.contains("Description of Note taker"), "{s}");
    h.type_text(" Now better.");
    click_confirm(&mut h, "Save", "Close");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::SetDescription { bundle_id, description, .. } if bundle_id == "note-taker" && description.contains("Now better."))));
}

#[test]
fn the_shipped_rows_refused_actions_say_why() {
    let mut h = page();
    click_row(&mut h, "Basic agent", "⤓");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Export { bundle_id, .. } if bundle_id == "basic-agent")));
    let mut h = page();
    click_row(&mut h, "Basic agent", "⇗");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::AppAct { app_id, .. } if app_id == "flow")));
    let mut h = page();
    let s = click_row(&mut h, "Basic agent", "⊟");
    assert!(
        s.contains("Workflows that ship with the gateway can't be archived or deleted."),
        "{s}"
    );
    assert!(wf_cmds(&mut h).is_empty());
    let mut h = page();
    let s = click_row(&mut h, "Basic agent", "✎");
    assert!(
        s.contains("Workflows that ship with the gateway keep their own description."),
        "{s}"
    );
    assert!(!s.contains("Description of Basic agent"), "{s}");
}

#[test]
fn clicks_on_the_shared_imported_row() {
    let mut h = page();
    click_row(&mut h, "Team digest", "⤓");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Export { bundle_id, .. } if bundle_id == "team-digest")));
    let mut h = page();
    click_row(&mut h, "Team digest", "⇗");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::AppAct { path: Some(p), .. } if p.contains("team-digest"))));
    let mut h = page();
    let s = click_row(&mut h, "Team digest", "⊟");
    assert!(s.contains("Archive Team digest?"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "Team digest", "✎");
    assert!(s.contains("Description of Team digest"), "{s}");
    // Its Available to users toggle (off) → the web's route.
    let mut h = page();
    click_row(&mut h, "Team digest", "●─");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::SetAvailability { bundle_id, available: true, .. } if bundle_id == "team-digest")));
}

#[test]
fn clicks_on_an_older_versions_row_and_an_archived_row() {
    let mut h = page();
    h.click_text("Older versions");
    assert!(h.store.wf.older.get_untracked(), "the switch is on");
    let s = h.turns(2);
    assert!(s.contains("↳ older version"), "{s}");
    click_row(&mut h, "↳ older version", "⤓");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::Export { bundle_id, version, .. } if bundle_id == "note-taker" && version == "1.1.0")));
    click_row(&mut h, "↳ older version", "⇗");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::AppAct { path: Some(p), .. } if p.contains("version=1.1.0"))));
    let s = click_row(&mut h, "↳ older version", "⊟");
    assert!(s.contains("Archive Note taker 1.1.0?"), "{s}");
    click_confirm(&mut h, "Archive", "Cancel");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::Archive { bundle_id, version, .. } if bundle_id == "note-taker" && version == "1.1.0")));
    // The archived row (Show archived lists it): Unarchive at once.
    let mut h = page();
    click_row(&mut h, "Draft helper", "⤒");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Unarchive { bundle_id, .. } if bundle_id == "draft-helper")));
    let mut h = page();
    click_row(&mut h, "Draft helper", "⤓");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Export { bundle_id, .. } if bundle_id == "draft-helper")));
    let mut h = page();
    click_row(&mut h, "Draft helper", "⇗");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::AppAct { path: Some(p), .. } if p.contains("draft-helper"))));
    let mut h = page();
    let s = click_row(&mut h, "Draft helper", "✎");
    assert!(s.contains("Description of Draft helper"), "{s}");
}

#[test]
fn the_head_and_the_switches_are_clickable() {
    let mut h = page();
    h.click_text(" ↻ ");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::Load(_))));
    let mut h = page();
    let s = h.click_text("Import .flow");
    assert!(
        s.contains("Install a .flow bundle: files on THIS machine"),
        "{s}"
    );
    h.type_text("/tmp/a.flow");
    click_confirm(&mut h, "Import", "Close");
    assert!(wf_cmds(&mut h).iter().any(
        |c| matches!(c, WfCmd::Import { paths, .. } if paths == &vec!["/tmp/a.flow".to_string()])
    ));
    let mut h = page();
    h.click_text("●─ Drafts");
    assert!(h.store.wf.drafts.get_untracked());
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Load(l) if l.drafts)));
    let mut h = page();
    h.click_text("●─ Show archived");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Load(l) if l.archived)));
}

#[test]
fn import_with_a_path_asks_before_close_drops_it() {
    let mut h = page();
    h.click_text("Import .flow");
    h.type_text("/tmp/a.flow");
    let s = click_confirm(&mut h, "Close", "Import");
    assert!(s.contains("Discard changes?"), "{s}");
    assert!(wf_cmds(&mut h).is_empty());
}

#[test]
fn the_defaults_pickers_and_streamed_replies() {
    // The picker: click it, pick Note taker, Enter → saved at once.
    let mut h = page();
    h.click_text("Gateway default: Basic agent 0.0.5");
    let s = h.turns(2);
    assert!(s.contains("Note taker 1.2.0"), "the popup is open:\n{s}");
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    h.key(b"\r");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::SaveDefault { iface, value } if iface == "abstractcode.agent.v1" && value == "note-taker@1.2.0:f1")));
    // Streamed replies (on) → off.
    let mut h = page();
    h.click_text("━● Streamed replies");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::SetStreaming { on: false })));
}

#[test]
fn the_broken_sections_archive_button() {
    let mut h = page();
    click_on_line(&mut h, "old-flow", 0, "Archive 2");
    assert!(wf_cmds(&mut h).iter().any(|c| matches!(c, WfCmd::ArchiveBroken { bundle_id, versions, .. } if bundle_id == "old-flow" && versions.len() == 2)));
}

#[test]
fn hovering_a_glyph_shows_the_web_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.starts_with(" Note taker ")
                .then(|| l.rfind('⤓').map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("Note taker's export glyph");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Export Note taker as a .flow file  (x)"), "{s}");
}

#[test]
fn the_keyboard_reaches_every_row_action() {
    // ↓ to Note taker, then its keys; Space flips availability on a shared row.
    let mut h = page();
    h.key(b" ");
    assert!(wf_cmds(&mut h).iter().any(
        |c| matches!(c, WfCmd::SetAvailability { bundle_id, .. } if bundle_id == "basic-agent")
    ));
    for _ in 0..3 {
        h.key(b"\x1b[B");
    }
    h.key(b"x");
    assert!(wf_cmds(&mut h)
        .iter()
        .any(|c| matches!(c, WfCmd::Export { bundle_id, .. } if bundle_id == "note-taker")));
    let s = h.key(b"d");
    assert!(s.contains("Archive Note taker?"), "{s}");
}

#[test]
fn the_description_form_survives_the_reload_it_causes() {
    let mut h = page();
    click_row(&mut h, "Note taker", "✎");
    h.store
        .wf
        .data
        .set(Loadable::Ready(workflows_from_payload(&bundles()).unwrap()));
    let s = h.turns(3);
    assert!(s.contains("Description of Note taker"), "{s}");
}

#[test]
fn a_user_sees_no_availability_column_nor_defaults() {
    let mut h = harness((140, 50), Mount::Page(page_view));
    h.identity("alice", false);
    h.store
        .wf
        .data
        .set(Loadable::Ready(workflows_from_payload(&bundles()).unwrap()));
    let s = h.turns(3);
    assert!(!s.contains("Available to users"), "{s}");
    assert!(!s.contains("Default workflow per app"), "{s}");
    assert!(s.contains("⚠ Broken workflows"), "{s}");
}

#[test]
fn fits_at_80x24() {
    let mut h = page_sized((80, 24));
    let s = h.turns(2);
    assert!(
        s.contains("Basic agent") && s.contains("Import .flow"),
        "{s}"
    );
    h.assert_fits();
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
    let d = workflows_from_payload(&bundles()).unwrap();
    let nt = d.rows.iter().find(|r| r.bundle_id == "note-taker").unwrap();
    let acts = workflows::row_actions(nt, nt.latest, false, true);
    let tip = |id: &str| {
        acts.iter()
            .find(|a| a.id == id)
            .and_then(|a| a.tooltip.clone())
            .unwrap()
    };
    assert_eq!(tip("export"), w("export_tip").replace("{n}", "Note taker"));
    assert_eq!(tip("open"), w("open_tip").replace("{n}", "Note taker"));
    assert_eq!(
        tip("archive"),
        w("archive_tip").replace("{n}", "Note taker")
    );
    assert_eq!(tip("edit"), w("edit_tip").replace("{n}", "Note taker"));
    let older = nt
        .versions
        .iter()
        .position(|v| v.version == "1.1.0")
        .unwrap();
    let oacts = workflows::row_actions(nt, older, true, true);
    assert_eq!(
        oacts[0].tooltip.clone().unwrap(),
        w("export_tip").replace("{n}", "Note taker 1.1.0")
    );
    let dh = d
        .rows
        .iter()
        .find(|r| r.bundle_id == "draft-helper")
        .unwrap();
    let un = workflows::row_actions(dh, dh.latest, false, true)
        .into_iter()
        .find(|a| a.id == "unarchive")
        .unwrap();
    assert_eq!(
        un.tooltip.unwrap(),
        w("unarchive_tip").replace("{n}", "Draft helper")
    );
    assert_eq!(
        workflows::archive_question("Note taker"),
        w("archive_confirm").replace("{n}", "Note taker")
    );
    assert_eq!(workflows::RELOAD_TIP, w("reload_tip"));
    assert_eq!(workflows::IMPORT_LABEL, w("import_label"));
    assert_eq!(workflows::IMPORT_TIP, w("import_tip"));
    assert_eq!(workflows::SEARCH_PLACEHOLDER, w("search_placeholder"));
    assert_eq!(workflows::BROKEN_ARCHIVE_TIP, w("broken_archive_tip"));
    assert_eq!(
        workflows::broken_actions(&d.broken[0])[0]
            .tooltip
            .clone()
            .unwrap(),
        w("broken_archive_tip")
    );
    use abstractgateway_console::store::workflows_page as wp;
    assert_eq!(wp::PURPOSE, w("purpose"));
    assert_eq!(wp::AVAILABLE_HELP, w("available_help"));
    assert_eq!(wp::BROKEN_SENTENCE, w("broken_sentence"));
    assert_eq!(wp::STREAMING_LABEL, w("streaming_label"));
    assert_eq!(wp::STREAMING_HELP, w("streaming_help"));
    assert_eq!(workflows::OTHER_TYPES, w("other_types"));
    assert_eq!(workflows::SETTINGS, w("settings"));
    for k in ["drafts", "older", "archived"] {
        let label = w(&format!("switch_{k}"));
        let mut h = page();
        let s = h.turns(1);
        assert!(s.contains(&format!("●─ {label}")), "{label}:\n{s}");
    }
    let mut h = page();
    let s = h.turns(1);
    for k in [
        "col_name",
        "col_what",
        "col_version",
        "col_source",
        "col_usedby",
        "col_available",
    ] {
        assert!(s.contains(&w(k)), "{k}:\n{s}");
    }
}
