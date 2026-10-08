//! Workflows (R7.2): snapshot tests per screen state (hermetic, the
//! gateway's payload shapes) and drive tests per action against the live
//! scratch gateway (ignored by default):
//!
//!   R7W1_URL=http://127.0.0.1:18785 R7W1_TOKEN=r7w1-admin-token-0000 \
//!   R7W1_SHOTS_DIR=<dir> cargo test --test r7w1_workflows -- --ignored --test-threads 1

mod r7w1;

use abstractgateway_console::store::workflows_page::{
    defaults_from_payload, workflows_from_payload,
};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, workflows};
use abstractgateway_console::worker::workflows::WfCmd;
use abstractgateway_console::worker::Cmd;
use r7w1::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    workflows::view(cx, ctx, &t)
}

fn version(owner: &str, id: &str, name: &str, v: &str, source: &str, extra: Value) -> Value {
    let mut it = json!({"bundle_id": id, "bundle_version": v, "owner": {"kind": owner}, "is_draft": false,
        "archived": false, "available": true, "source": source,
        "description": format!("{name}: does the thing it was written for, explained in a sentence long enough to wrap."),
        "default_entrypoint": "f1", "created_at": "2026-10-01T10:00:00Z", "version_channel": "",
        "entrypoints": [{"flow_id": "f1", "name": name, "description": "", "interfaces": ["abstractcode.agent.v1"]}],
        "actions": {"can_archive": source != "shipped", "can_set_availability": owner == "gateway"}});
    for (k, val) in extra.as_object().unwrap() {
        it[k] = val.clone();
    }
    it
}

fn bundles() -> Value {
    json!({"items": [
        version("gateway", "basic-agent", "Basic agent", "0.0.5", "shipped", json!({})),
        version("gateway", "basic-agent", "Basic agent", "0.0.4", "shipped", json!({})),
        version("gateway", "team-digest", "Team digest", "2.0.0", "imported", json!({"available": false})),
        version("user", "note-taker", "Note taker", "1.2.0", "imported", json!({})),
        version("user", "draft-helper", "Draft helper", "0.1.0", "published", json!({"archived": true})),
    ], "skipped": [
        {"bundle_id": "old-flow", "bundle_version": "0.1.0", "reason": "needs abstractruntime >= 0.9", "path": "/x/old-flow@0.1.0.flow", "can_archive": true, "archived": false}
    ]})
}

fn defaults() -> Value {
    json!({"writable": true, "agents": {"default_workflow": {
        "abstractcode.agent.v1": {"label": "AbstractCode — chat agent", "help": "The agent AbstractCode runs.", "group": "apps",
            "state": "builtin", "value": "basic-agent@0.0.5:f1", "source": "default", "default": "basic-agent@0.0.5:f1",
            "reason": null, "resolved": {"name": "Basic agent", "bundle_version": "0.0.5"},
            "eligible": [{"value": "basic-agent@0.0.5:f1", "name": "Basic agent", "bundle_version": "0.0.5"},
                         {"value": "note-taker@1.2.0:f1", "name": "Note taker", "bundle_version": "1.2.0"}]},
        "abstractassistant.agent.v1": {"label": "Assistant", "help": "The Assistant's agent.", "group": "apps",
            "state": "broken", "value": "gone@1:f1", "source": "stored", "default": "",
            "reason": "Broken: gone@1 is not installed — pick another workflow or the gateway default.", "eligible": []},
        "batch.map.v1": {"label": "Batch map-reduce", "help": "Runs over many inputs.", "group": "other", "state": "none",
            "value": "", "source": "default", "default": "", "reason": null, "eligible": []}
    }, "streaming_default": {"value": true}}})
}

fn page(size: (i32, i32), admin: bool) -> r7w1::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.identity(if admin { "admin" } else { "alice" }, admin);
    h.store
        .wf
        .data
        .set(Loadable::Ready(workflows_from_payload(&bundles()).unwrap()));
    let mut d = defaults();
    d["writable"] = json!(admin);
    h.store
        .wf
        .defaults
        .set(Loadable::Ready(defaults_from_payload(&d).unwrap()));
    h.turns(3);
    h
}

#[test]
fn groups_rows_and_the_web_words() {
    for size in SIZES {
        let mut h = page(size, true);
        let s = h.shoot("workflows");
        assert!(
            s.contains("Workflows") && s.contains("Bundles, versions, import and export"),
            "{s}"
        );
        assert!(s.contains("Shared with everyone"), "{s}");
        // The table windows itself on a short page: End reaches "Mine".
        let s = if size.1 < 30 { h.key(b"\x1b[F") } else { s };
        assert!(s.contains("Mine"), "{s}");
        let s = h.key(b"\x1b[H");
        assert!(
            s.contains("Basic agent") && s.contains("basic-agent"),
            "{s}"
        );
        assert!(s.contains("0.0.5 +1 older"), "{s}");
        // R15: the switches are Toggles labelled by the feature.
        assert!(
            s.contains("●─ Drafts")
                && s.contains("●─ Older versions")
                && s.contains("●─ Show archived"),
            "{s}"
        );
        assert!(
            s.contains(if size.0 >= 110 {
                "Available to users"
            } else {
                "Available"
            }),
            "admin column:\n{s}"
        );
        // The Available to users cells: one on, one off.
        assert!(s.contains("━●") && s.matches("●─").count() >= 4, "{s}");
        if size.0 >= 110 {
            assert!(
                s.contains("Shipped") && s.contains("AbstractCode —") && s.contains("From"),
                "{s}"
            );
        }
        assert!(!s.contains('…'), "cut:\n{s}");
        h.assert_fits();
    }
    // The Broken workflows section follows on the same page (scrolled into
    // view on a short terminal).
    let mut h = page((120, 60), true);
    let s = h.text();
    assert!(s.contains("⚠ Broken workflows"), "{s}");
}

#[test]
fn a_user_sees_no_availability_column() {
    let mut h = page((120, 40), false);
    let s = h.text();
    assert!(!s.contains("Available to users"), "{s}");
}

#[test]
fn rows_never_expand_and_older_versions_are_their_own_rows() {
    // R8.1: no ▸ unfold — R15: the row's actions are glyph buttons in its
    // own Actions cell; Enter = the first one (Export), nothing expands.
    // "Older versions" lists each older version as its own row.
    let mut h = page((80, 24), true);
    h.sent();
    let s = h.key(b"\r");
    assert!(
        !s.contains("0.0.5 — published"),
        "Enter expands nothing:\n{s}"
    );
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Workflows(WfCmd::Export { bundle_id, .. }) if bundle_id == "basic-agent")));
    let row = s
        .lines()
        .find(|l| l.contains("Basic agent"))
        .unwrap_or_else(|| panic!("{s}"));
    assert!(row.contains("⤓") && row.contains("⇗"), "{s}");
    // At 80 columns Source / Used by ride in the name cell.
    assert!(s.contains("Shipped · AbstractCode —"), "{s}");
    h.shoot("workflows-actions");
    h.key(b"o");
    let s = h.text();
    assert!(
        s.contains("↳ older version") && s.contains("0.0.4"),
        "older versions:\n{s}"
    );
    assert!(s.contains("published · 2026-10-01"), "{s}");
}

#[test]
fn availability_switch_sends_the_web_route() {
    let mut h = page((120, 40), true);
    h.sent();
    h.key(b" ");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::Workflows(WfCmd::SetAvailability { bundle_id, available: false, .. }) if bundle_id == "basic-agent")),
        "{sent:?}"
    );
}

/// Click the `label` button of an open dialog.
fn click_confirm(h: &mut r7w1::Harness, label: &str, other: &str) -> String {
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

#[test]
fn shipped_cannot_be_archived_and_mine_asks_inline() {
    let mut h = page((120, 40), true);
    h.sent();
    let s = h.key(b"d");
    assert!(
        s.contains("Workflows that ship with the gateway can't be archived or deleted."),
        "{s}"
    );
    assert!(h.sent().is_empty());
    // Down to Note taker (Mine): Team digest, then Draft helper (archived), Note taker.
    h.store.wf.sel.set(5);
    h.turns(2);
    let s = h.key(b"d");
    assert!(
        s.contains("Archive Note taker? It disappears from lists and can't start new runs"),
        "{s}"
    );
    assert!(h.sent().is_empty(), "nothing before [Archive]");
    // R15 F1: the confirmation's [Archive] button does it.
    click_confirm(&mut h, "Archive", "Cancel");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Workflows(WfCmd::Archive { bundle_id, .. }) if bundle_id == "note-taker")));
}

#[test]
fn defaults_tab_options_state_and_streaming() {
    // R15: the defaults are a card on the Workflows page (no tab), below the
    // table (scrolled into view on a short terminal).
    for size in [(120, 60), (100, 60)] {
        let mut h = page(size, true);
        let s = h.shoot("workflows-defaults");
        assert!(s.contains("Default workflow per app"), "{s}");
        assert!(s.contains("When an app asks for “an agent”"), "{s}");
        assert!(
            s.contains("AbstractCode — chat agent")
                && s.contains("Gateway default: Basic agent 0.0.5"),
            "{s}"
        );
        assert!(s.contains("gone@1:f1 (not installed)"), "{s}");
        assert!(s.contains("Broken: gone@1 is not installed"), "{s}");
        // R15 D1: the other types are a visible section, never folded.
        assert!(
            s.contains("Other workflow types") && s.contains("Batch map-reduce"),
            "{s}"
        );
        assert!(s.contains("━● Streamed replies"), "{s}");
        h.assert_fits();
    }
}

#[test]
fn picking_a_default_saves_at_once() {
    let mut h = page((120, 60), true);
    h.sent();
    let s = h.click_text("Gateway default: Basic agent 0.0.5");
    assert!(s.contains("Note taker 1.2.0"), "the picker's popup:\n{s}");
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    h.key(b"\r");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::Workflows(WfCmd::SaveDefault { iface, value }) if iface == "abstractcode.agent.v1" && value == "note-taker@1.2.0:f1")),
        "{sent:?}"
    );
}

#[test]
fn a_user_never_sees_the_default_workflow_per_app() {
    // R8.1: "Default workflow per app" is admin-only — hidden and never
    // read for anyone else; the Broken workflows section still shows.
    let mut h = page((120, 60), false);
    let s = h.text();
    assert!(!s.contains("Default workflow per app"), "{s}");
    assert!(s.contains("⚠ Broken workflows"), "{s}");
    h.sent();
    workflows::refresh_for_tests(&h.store, &h.tx);
    let sent = h.sent();
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::Workflows(WfCmd::Load(l)) if !l.defaults)),
        "a non-admin's refresh does not read the defaults: {sent:?}"
    );
}

#[test]
fn broken_tab_lists_and_archives() {
    let mut h = page((120, 60), true);
    let s = h.shoot("workflows-broken");
    assert!(
        s.contains("1 workflow, 1 version the gateway could not load."),
        "{s}"
    );
    assert!(
        s.contains("These bundle files are on disk but the gateway cannot run them"),
        "{s}"
    );
    assert!(s.contains("needs abstractruntime >= 0.9"), "{s}");
    h.sent();
    // R15: its Archive button.
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("old-flow") && l.contains("Archive"))
        .unwrap_or_else(|| panic!("{s}"));
    let x = line[..line.rfind("Archive").unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Workflows(WfCmd::ArchiveBroken { bundle_id, .. }) if bundle_id == "old-flow")));
}

// ---------------------------------------------------------------- live

fn live_page(size: (i32, i32)) -> Option<(r7w1::Harness, String, String)> {
    let (url, token) = live_env()?;
    let mut h = live(size, Mount::Page(page_view), &url, &token);
    workflows::refresh_for_tests(&h.store, &h.tx);
    until_row(&mut h, "basic-agent", None);
    Some((h, url, token))
}

fn until_row(h: &mut r7w1::Harness, bundle_id: &str, archived: Option<bool>) {
    let id = bundle_id.to_string();
    h.until(bundle_id, move |h, _| {
        h.store.wf.data.with_untracked(|d| {
            d.ready().is_some_and(|d| {
                d.rows
                    .iter()
                    .any(|r| r.bundle_id == id && archived.is_none_or(|a| r.archived == a))
            })
        })
    });
}

/// Wait until `needle` is on screen or was said as a toast (R15: verified
/// successes are toasts).
fn until_said(h: &mut r7w1::Harness, needle: &str) {
    let n = needle.to_string();
    h.until(needle, move |_, s| {
        s.contains(&n) || ui::w::notify::toasts().iter().any(|t| t.contains(&n))
    });
}

fn select(h: &mut r7w1::Harness, bundle_id: &str) {
    until_row(h, bundle_id, None);
    let q = h.store.wf.query.get_untracked();
    let idx = h.store.wf.data.with_untracked(|d| {
        let d = d.ready().unwrap();
        let mut i = 0;
        for (_, rows) in d.groups(&q) {
            i += 1;
            for r in rows {
                if r.bundle_id == bundle_id {
                    return Some(i);
                }
                i += 1;
            }
        }
        None
    });
    h.store.wf.sel.set(idx.expect(bundle_id));
    h.turns(2);
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_availability_archive_export_import_default() {
    let Some((mut h, url, token)) = live_page((120, 40)) else {
        return;
    };
    gw(
        "PUT",
        &url,
        &token,
        "/admin/workflows/team-digest/availability",
        Some(json!({"available": true})),
    );
    gw(
        "POST",
        &url,
        &token,
        "/bundles/team-digest/unarchive",
        Some(json!({})),
    );
    workflows::refresh_for_tests(&h.store, &h.tx);
    until_row(&mut h, "team-digest", Some(false));
    h.shoot("live-workflows");
    // Available to users: off, verified on the gateway, then on.
    select(&mut h, "team-digest");
    h.key(b" ");
    until_said(&mut h, "Team digest is hidden from users");
    let alice = gw(
        "GET",
        &url,
        "r7w1-alice-token-0001",
        "/bundles?all_versions=true",
        None,
    );
    assert!(
        !alice["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|i| i["bundle_id"] == "team-digest"),
        "hidden from users"
    );
    select(&mut h, "team-digest");
    h.key(b" ");
    until_said(&mut h, "Team digest is available to users again.");
    // Archive (inline confirm) then Unarchive via Show archived.
    select(&mut h, "team-digest");
    h.key(b"d");
    h.until_text("Archive Team digest?");
    click_confirm(&mut h, "Archive", "Cancel");
    until_said(&mut h, "Archived Team digest. Turn on “Show archived”");
    let v = gw(
        "GET",
        &url,
        &token,
        "/bundles?all_versions=true&include_archived=1",
        None,
    );
    assert!(
        v["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|i| i["bundle_id"] == "team-digest" && i["archived"] == true),
        "{v}"
    );
    h.key(b"h");
    h.until_text("━● Show archived");
    until_row(&mut h, "team-digest", Some(true));
    select(&mut h, "team-digest");
    h.key(b"d");
    until_said(
        &mut h,
        "Team digest is back in the lists and can start runs again.",
    );
    // Export then re-import the same file (installed again, same version).
    let dir = std::env::temp_dir().join(format!("r7w1-wf-{}", std::process::id()));
    h.tx.send(Cmd::Workflows(WfCmd::Export {
        bundle_id: "team-digest".into(),
        version: "2.0.0".into(),
        dir: dir.clone(),
    }))
    .unwrap();
    until_said(&mut h, "Exported team-digest@2.0.0 to");
    let file = dir.join("team-digest@2.0.0.flow");
    assert!(std::fs::metadata(&file).unwrap().len() > 0);
    h.tx.send(Cmd::Workflows(WfCmd::Import {
        paths: vec![file.display().to_string()],
        list: Default::default(),
    }))
    .unwrap();
    h.until("import result", |_h, s| {
        let said = |n: &str| s.contains(n) || ui::w::notify::toasts().iter().any(|t| t.contains(n));
        said("Installed") || said("Failed —")
    });
    h.shoot("live-workflows-import");
    // Default workflow per app (the card's picker): pick the first
    // non-default option — saved at once.
    h.until_text("AbstractCode — chat agent");
    h.click_text("Gateway default:");
    h.key(b"\x1b[B");
    h.key(b"\r");
    h.until_text("Saved");
    let rc = gw("GET", &url, &token, "/admin/runtime-config", None);
    let row = &rc["agents"]["default_workflow"]["abstractcode.agent.v1"];
    assert_eq!(row["source"], "stored", "{row}");
    // Back to the gateway default.
    let s = h.turns(2);
    let picked = s
        .lines()
        .find(|l| l.contains("AbstractCode — chat agent"))
        .and_then(|l| l.split('▐').nth(1))
        .map(|v| v.trim_end_matches(['▌', '▾', ' ']).trim().to_string())
        .unwrap_or_default();
    h.click_text(&picked);
    h.key(b"\x1b[A");
    h.key(b"\x1b[A");
    h.key(b"\r");
    h.turns(10);
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_capture_80x24() {
    let Some((mut h, _url, _token)) = live_page((80, 24)) else {
        return;
    };
    h.shoot("live-workflows");
    h.key(b"\t");
    h.until_text("AbstractCode —");
    h.shoot("live-workflows-defaults");
}
