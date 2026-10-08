//! R15 (DESIGN-TUI.md §3.6): a synthesized mouse click for EVERY Runtimes
//! control — the head's account chip and ↻, a runtime row (choose) and its
//! Workspace link, the inspector's four tabs and ↻, the Runs toolbar
//! (status dropdown, search, root runs only) and each run's Inspect /
//! Steer / Cancel, the pager, an artifact and a log file (their dialogs),
//! each cache's Purge… and each stale row's Forget plus Forget all stale —
//! through the real input pipeline. The meta-test enumerates
//! `runtimes::head_actions` / `inventory_actions` / `run_actions` /
//! `cache_actions` / `detail_actions` over the fixture: an action without
//! a click test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_runtimes.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::{
    data_homes_from_payload, log_files_from_payload, runs_from_payload, runtimes_from_payload,
    ArtifactRow, ArtifactsData, Loadable, PurgeCounts, PurgePlan, RunScope, RunsData,
    RuntimeFilter,
};
use abstractgateway_console::ui::{self, runtimes};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    runtimes::view(cx, ctx, &t)
}

fn inventory() -> Value {
    json!({"runtimes": [
        {"kind": "default", "tenant_id": "default", "runtime_id": "default",
         "label": "Gateway default runtime",
         "owners": [{"user_id": "admin", "enabled": true}],
         "data_dir": "/srv/gw/runtime", "size_bytes": 356515840u64},
        {"kind": "user", "tenant_id": "default", "runtime_id": "alice",
         "label": "alice", "owners": [{"user_id": "alice", "enabled": true}],
         "data_dir": "/srv/gw/runtime/users/alice", "size_bytes": 12582912u64},
        {"kind": "user", "tenant_id": "default", "runtime_id": "team",
         "label": "team", "owners": [{"user_id": "bob", "enabled": true}, {"user_id": "carol", "enabled": true}],
         "data_dir": "/srv/gw/runtime/users/team", "size_bytes": 2048u64},
        {"kind": "entity", "tenant_id": "default", "runtime_id": "runtime_testor",
         "label": "Testor", "entity": "testor", "owners": [{"user_id": "testor", "enabled": true}],
         "state": "asleep", "liveness": "alive",
         "data_dir": "/srv/gw/runtime/entities/testor", "size_bytes": 33487609u64}
    ]})
}

fn runs(offset: u32, has_more: bool) -> RunsData {
    RunsData {
        scope: RunScope::Own,
        rows: runs_from_payload(&json!({"items": [
            {"run_id": "run-live-1", "workflow_id": "basic-agent", "status": "running",
             "current_node": "llm", "session_id": "s-12", "updated_at": "2026-10-08T07:58:00"},
            {"run_id": "run-done-2", "workflow_id": "team-digest", "status": "completed",
             "current_node": "end", "session_id": "s-13", "updated_at": "2026-10-08T07:40:00"}
        ]})),
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset,
        has_more,
    }
}

fn homes() -> Value {
    json!({"homes": [
        {"name": "prompt-kv", "path": "/srv/gw/runtime/cache/kv", "kind": "kv-cache", "owner": "abstractgateway",
         "safe_to_purge": true, "description": "Prompt KV snapshots", "exists": true, "size_bytes": 1048576u64},
        {"name": "hf-hub", "path": "/home/me/.cache/huggingface/hub", "kind": "models", "owner": "abstractcore",
         "safe_to_purge": true, "description": "Downloaded model weights", "exists": true, "size_bytes": 4194304u64},
        {"name": "old-scratch", "path": "/tmp/gone/a", "kind": "kv-cache", "owner": "abstractgateway",
         "safe_to_purge": true, "description": "", "exists": false},
        {"name": "old-scratch-2", "path": "/tmp/gone/b", "kind": "kv-cache", "owner": "abstractgateway",
         "safe_to_purge": true, "description": "", "exists": false}
    ]})
}

fn logs() -> Value {
    json!({"homes": [{"home": "gateway-logs", "files": [
        {"name": "gateway.log", "size_bytes": 2048u64, "modified_at": "2026-10-08T07:59:00"},
        {"name": "launcher.log", "size_bytes": 512u64, "modified_at": "2026-10-08T06:00:00"}
    ]}]})
}

fn artifacts() -> ArtifactsData {
    let a = |name: &str, kind: &str, id: &str| ArtifactRow {
        name: name.into(),
        kind: kind.into(),
        size_bytes: Some(1200),
        run_id: "run-done-2".into(),
        created_at: "2026-10-08T07:41:00".into(),
        artifact_id: id.into(),
        content_type: "text/markdown".into(),
        content_path: "/srv/gw/artifacts/a".into(),
        workflow_id: "team-digest".into(),
        session_id: "s-13".into(),
    };
    ArtifactsData {
        rows: vec![
            a("digest.md", "markdown", "a1"),
            a("chart.png", "image", "a2"),
        ],
        total: 2,
        has_more: false,
        offset: 0,
        modality: String::new(),
        query: String::new(),
    }
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_runtimes.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn page_sized(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&inventory())));
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_sized((130, 44))
}

/// The page with the default runtime chosen and its runs loaded.
fn chosen() -> r8w4::Harness {
    let mut h = page();
    click_row(&mut h, "default", "default");
    h.store.runs.set(Loadable::Ready(runs(0, false)));
    h.turns(2);
    h.sent();
    h
}

/// Click `needle` on the row whose line starts with `name` (the last
/// occurrence on the line).
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

/// Click `needle` on the first line holding `anchor`.
fn click_on(h: &mut r8w4::Harness, anchor: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(anchor))
        .unwrap_or_else(|| panic!("{anchor:?}:\n{screen}"));
    let byte = line
        .find(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on the {anchor:?} line:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the `label` button of an open dialog (its row holds `other`).
fn click_confirm(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the last ` label ` button on screen (a dialog's footer).
fn click_last(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) || l.ends_with(&format!(" {label}")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}]:\n{screen}"));
    let b = line.rfind(&format!(" {label}")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click a tab of the inspector (the segment on the "▷ Runtime" line).
fn click_tab(h: &mut r8w4::Harness, tab: &str) -> String {
    click_on(h, "▷ Runtime", tab)
}

/// The worker's dry-run answer for `name`.
fn dry_run_answers(h: &mut r8w4::Harness, name: &str, files: Option<u64>, bytes: Option<u64>) {
    h.store.purge_plan.set(Some(PurgePlan {
        name: name.into(),
        result: Ok(PurgeCounts {
            files_deleted: files,
            bytes_freed: bytes,
        }),
    }));
    h.turns(2);
}

fn sent_matching(h: &mut r8w4::Harness, f: impl Fn(&Cmd) -> bool) -> Vec<Cmd> {
    h.sent().into_iter().filter(|c| f(c)).collect()
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    for r in runtimes_from_payload(&inventory()) {
        for a in runtimes::inventory_actions(&r) {
            out.insert((r.runtime_id.clone(), a.id));
        }
    }
    let filter = RuntimeFilter {
        account: "alice".into(),
        tenant_id: "default".into(),
    };
    for a in runtimes::head_actions(Some(&filter)) {
        out.insert(("head".into(), a.id));
    }
    for a in runtimes::detail_actions() {
        out.insert(("detail".into(), a.id));
    }
    for r in runs(0, false).rows {
        for a in runtimes::run_actions(&r, true) {
            out.insert((r.run_id.clone(), a.id));
        }
    }
    for stale in [false, true] {
        for a in runtimes::cache_actions(stale) {
            out.insert((format!("cache:{stale}"), a.id));
        }
    }
    out.insert(("cache".into(), runtimes::forget_all_action(2).id));
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("default", "workspaces"),
        ("alice", "workspaces"),
        ("runtime_testor", "workspaces"),
        ("head", "account"),
        ("head", "reload"),
        ("head", "retained"),
        ("detail", "reload_detail"),
        ("run-live-1", "inspect"),
        ("run-live-1", "steer"),
        ("run-live-1", "cancel"),
        ("run-done-2", "inspect"),
        ("cache:false", "purge"),
        ("cache:true", "forget"),
        ("cache", "forget_all"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_runtimes_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Runtimes actions without a click test: {missing:?}"
    );
}

#[test]
fn the_page_says_the_webs_words() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "Runtimes",
        "Each user's own data plane: runs, flows, sessions and memory",
        "A runtime is a user's own data plane",
        " ↻",
        "Runtime ",
        "Kind",
        "Owner",
        "State",
        "Size",
        "Workspace",
        "Eligible workspaces",
        "bob, carol",
        "None",
        "asleep",
        "Select a runtime above — click a row — to load its runs and cache.",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    h.assert_fits();
    let mut h = chosen();
    let s = h.turns(1);
    for needle in [
        "▷ Runtime default",
        "Runs",
        "Artifacts",
        "Cache",
        "Logs",
        "The gateway default runtime (admin plane) · 340.0 MiB.",
        "all statuses",
        "Search runs — run id, workflow, session…",
        "━● root runs only",
        "Node",
        "Session",
        "Updated",
        "Actions",
        "Inspect",
        "Steer",
        "Cancel",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    assert!(!s.contains("Select a runtime above"), "{s}");
    // DESIGN D3: the runtime knobs live on the pages that own them.
    assert!(!s.contains("Runtime knobs"), "{s}");
    assert!(
        !sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuntimeConfig))
            .iter()
            .any(|_| true)
    );
    h.assert_fits();
}

#[test]
fn a_click_on_a_runtime_row_chooses_it() {
    let mut h = page();
    let s = click_row(&mut h, "alice", "alice");
    let loads = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(&loads[..], [Cmd::LoadRuns { scope: RunScope::Plane { runtime_id, .. }, .. }] if runtime_id == "alice"),
        "{loads:?}"
    );
    let s2 = h.turns(1);
    assert!(s2.contains("▷ Runtime alice"), "{s}\n{s2}");
    assert!(s2.contains("this user's plane"), "{s2}");
    // A read-only plane: the web's note, no toolbar, no run buttons.
    h.store.runs.set(Loadable::Ready(RunsData {
        scope: RunScope::of_runtime(&runtimes_from_payload(&inventory())[1]),
        ..runs(0, false)
    }));
    let s = h.turns(2);
    assert!(
        s.contains("Read-only view — newest runs on this plane."),
        "{s}"
    );
    let row = s
        .lines()
        .find(|l| l.starts_with(" run-live-1 "))
        .expect("row");
    assert!(
        !row.contains("Inspect") && !s.contains("root runs only"),
        "{s}"
    );
    assert!(!s.contains("Actions"), "{s}");
}

#[test]
fn the_head_reloads_and_drops_the_account_filter() {
    let mut h = page();
    click_on(&mut h, "Runtimes", "↻");
    assert!(!sent_matching(&mut h, |c| matches!(
        c,
        Cmd::LoadRuntimes | Cmd::LoadRuntimesFor { .. }
    ))
    .is_empty());
    let mut h = page();
    h.store.runtime_filter.set(Some(RuntimeFilter {
        account: "alice".into(),
        tenant_id: "default".into(),
    }));
    let s = h.turns(2);
    assert!(s.contains("Account: alice ×"), "{s}");
    click_on(&mut h, "Runtimes", "Account: alice ×");
    assert!(h.store.runtime_filter.get_untracked().is_none());
    assert!(!sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuntimes)).is_empty());
}

#[test]
fn the_workspace_links_open_the_workspaces_dialog() {
    for (row, title) in [
        ("default", "Eligible workspaces"),
        ("alice", "— alice"),
        ("runtime_testor", "— testor"),
    ] {
        let mut h = page();
        let label = if row == "default" {
            "Eligible workspaces"
        } else {
            "Workspaces"
        };
        click_row(&mut h, row, label);
        let s = h.turns(2);
        assert!(s.contains(title), "{row}: {title:?}\n{s}");
        assert!(s.contains("✕"), "a dialog:\n{s}");
    }
}

#[test]
fn the_tabs_load_their_own_data() {
    let mut h = chosen();
    click_tab(&mut h, "Artifacts");
    assert!(!sent_matching(&mut h, |c| matches!(c, Cmd::LoadArtifacts { .. })).is_empty());
    click_tab(&mut h, "Cache");
    assert_eq!(
        sent_matching(&mut h, |c| matches!(c, Cmd::LoadDataHomes { .. })).len(),
        2
    );
    click_tab(&mut h, "Logs");
    assert!(!sent_matching(&mut h, |c| matches!(c, Cmd::LoadLogs)).is_empty());
    let s = click_tab(&mut h, "Runs");
    assert!(s.contains("run-live-1"), "{s}");
    // The inspector's ↻: every tab reads again.
    click_on(&mut h, "▷ Runtime", "↻");
    assert!(!sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. })).is_empty());
}

#[test]
fn inspect_steer_and_cancel_by_mouse() {
    // Inspect (a live and a finished run).
    for run in ["run-live-1", "run-done-2"] {
        let mut h = chosen();
        click_row(&mut h, run, "Inspect");
        let s = h.turns(2);
        assert!(
            s.contains(&format!("Run {}", &run[..run.len().min(12)])),
            "{s}"
        );
        assert!(s.contains("Workflow") && s.contains("Session"), "{s}");
        let s = click_last(&mut h, "Close");
        let _ = s;
        let s = h.turns(2);
        assert!(!s.contains(&format!("Run {run}")), "closed:\n{s}");
    }
    // Steer: the web's sentence, type, Send guidance.
    let mut h = chosen();
    click_row(&mut h, "run-live-1", "Steer");
    let s = h.turns(2);
    assert!(s.contains("Steer run"), "{s}");
    assert!(
        s.contains("Guidance folds into run-live-1's next reasoning cycle"),
        "{s}"
    );
    h.type_text("finish now");
    click_confirm(&mut h, "Send guidance", "Cancel");
    let steer = sent_matching(&mut h, |c| matches!(c, Cmd::SteerRun { .. }));
    assert!(
        matches!(&steer[..], [Cmd::SteerRun { run_id, guidance }] if run_id == "run-live-1" && guidance == "finish now"),
        "{steer:?}"
    );
    // Cancel: the web's question, [Cancel run] [Cancel].
    let mut h = chosen();
    let s = click_row(&mut h, "run-live-1", "Cancel");
    let s = if s.contains("Cancel run run-live-1?") {
        s
    } else {
        h.turns(2)
    };
    assert!(
        s.contains("Cancel run run-live-1? Any in-flight work stops at the next tick."),
        "{s}"
    );
    click_confirm(&mut h, "Cancel run", "Cancel");
    let cancel = sent_matching(&mut h, |c| matches!(c, Cmd::CancelRun { .. }));
    assert!(
        matches!(&cancel[..], [Cmd::CancelRun { run_id }] if run_id == "run-live-1"),
        "{cancel:?}"
    );
}

#[test]
fn the_steer_form_guards_typed_guidance_and_survives_a_reload() {
    let mut h = chosen();
    click_row(&mut h, "run-live-1", "Steer");
    h.type_text("half a thought");
    // A runs reload lands under the open form.
    h.store.runs.set(Loadable::Ready(runs(0, false)));
    let s = h.turns(3);
    assert!(
        s.contains("Steer run") && s.contains("half a thought"),
        "{s}"
    );
    let s = h.esc();
    let s = if s.contains("Discard changes?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Discard changes?"), "{s}");
}

#[test]
fn the_runs_toolbar_by_mouse() {
    // Status dropdown.
    let mut h = chosen();
    let s = h.click_text("all statuses");
    assert!(
        s.contains("running") && s.contains("cancelled"),
        "the popup:\n{s}"
    );
    h.key(b"\x1b[B");
    h.key(b"\r");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(&l[..], [Cmd::LoadRuns { status, .. }] if status == "running"),
        "{l:?}"
    );
    // Search (Enter commits).
    let mut h = chosen();
    h.click_text("Search runs");
    h.type_text("coder");
    h.key(b"\r");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(&l[..], [Cmd::LoadRuns { query, .. }] if query == "coder"),
        "{l:?}"
    );
    // root runs only.
    let mut h = chosen();
    h.click_text("root runs only");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(
            &l[..],
            [Cmd::LoadRuns {
                root_only: false,
                ..
            }]
        ),
        "{l:?}"
    );
}

#[test]
fn the_pager_by_mouse() {
    let mut h = chosen();
    h.store.runs.set(Loadable::Ready(runs(0, true)));
    let s = h.turns(2);
    assert!(s.contains("1–2 · more"), "{s}");
    h.click_text("Next ›");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(&l[..], [Cmd::LoadRuns { offset: 100, .. }]),
        "{l:?}"
    );
    h.store.runs.set(Loadable::Ready(runs(100, false)));
    h.turns(2);
    h.click_text("‹ Prev");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(matches!(&l[..], [Cmd::LoadRuns { offset: 0, .. }]), "{l:?}");
}

fn on_tab(tab: &str) -> r8w4::Harness {
    let mut h = chosen();
    click_tab(&mut h, tab);
    h.store.artifacts.set(Loadable::Ready(artifacts()));
    h.store
        .data_homes
        .set(Loadable::Ready(data_homes_from_payload(&homes())));
    h.store
        .logs
        .set(Loadable::Ready(log_files_from_payload(&logs())));
    h.turns(3);
    h.sent();
    h
}

#[test]
fn an_artifact_opens_its_preview() {
    let mut h = on_tab("Artifacts");
    let s = h.turns(1);
    assert!(s.contains("Artifact") && s.contains("Created"), "{s}");
    click_row(&mut h, "digest.md", "digest.md");
    let s = h.turns(2);
    assert!(s.contains("markdown · text/markdown"), "{s}");
    assert!(!sent_matching(&mut h, |c| matches!(c, Cmd::LoadArtifactText { .. })).is_empty());
    click_last(&mut h, "Close");
    let s = h.turns(2);
    assert!(!s.contains("markdown · text/markdown"), "{s}");
}

#[test]
fn purge_and_forget_ask_the_webs_questions() {
    let mut h = on_tab("Cache");
    let s = h.turns(1);
    assert!(
        s.contains("Stale registrations (2, whole machine registry)"),
        "{s}"
    );
    click_row(&mut h, "prompt-kv", "Purge…");
    // The dry-run goes first; its accounting is the question.
    let d = sent_matching(&mut h, |c| matches!(c, Cmd::PurgeDryRun { .. }));
    assert!(
        matches!(&d[..], [Cmd::PurgeDryRun { name }] if name == "prompt-kv"),
        "{d:?}"
    );
    dry_run_answers(&mut h, "prompt-kv", Some(12), Some(4096));
    let s = h.turns(2);
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains(
            "Purge prompt-kv? This deletes the CONTENTS of prompt-kv: 12 files, 4.0 KiB freed."
        ),
        "{s}"
    );
    click_confirm(&mut h, "Purge", "Cancel");
    let p = sent_matching(&mut h, |c| matches!(c, Cmd::PurgeDataHome { .. }));
    assert!(
        matches!(&p[..], [Cmd::PurgeDataHome { name }] if name == "prompt-kv"),
        "{p:?}"
    );
    // A count the gateway did not report is unknown, never 0; a refused
    // dry-run purges nothing and says why.
    let mut h = on_tab("Cache");
    click_row(&mut h, "prompt-kv", "Purge…");
    dry_run_answers(&mut h, "prompt-kv", None, None);
    let s = h.turns(2);
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains("an unknown number of files, an") && flat.contains("unknown amount freed"),
        "{s}"
    );
    let mut h = on_tab("Cache");
    click_row(&mut h, "prompt-kv", "Purge…");
    h.store.purge_plan.set(Some(PurgePlan {
        name: "prompt-kv".into(),
        result: Err("the path is live".into()),
    }));
    let s = h.turns(2);
    assert!(!s.contains("Purge prompt-kv?"), "{s}");
    assert!(
        h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .contains("Nothing purged: the path is live"),
        "{:?}",
        h.store.notice.get_untracked()
    );
    assert!(sent_matching(&mut h, |c| matches!(c, Cmd::PurgeDataHome { .. })).is_empty());
    // One stale row.
    let mut h = on_tab("Cache");
    click_row(&mut h, "old-scratch", "Forget");
    let s = h.turns(2);
    assert!(
        s.contains("This removes the stale row old-scratch from the data-home registry."),
        "{s}"
    );
    click_confirm(&mut h, "Forget", "Cancel");
    let f = sent_matching(&mut h, |c| matches!(c, Cmd::ForgetDataHomes { .. }));
    assert!(
        matches!(
            &f[..],
            [Cmd::ForgetDataHomes {
                all_stale: false,
                ..
            }]
        ),
        "{f:?}"
    );
    // Every stale row.
    let mut h = on_tab("Cache");
    h.click_text("Forget all stale (2)");
    let s = h.turns(2);
    assert!(s.contains("This removes every stale registration"), "{s}");
    click_confirm(&mut h, "Forget", "Cancel");
    let f = sent_matching(&mut h, |c| matches!(c, Cmd::ForgetDataHomes { .. }));
    assert!(
        matches!(
            &f[..],
            [Cmd::ForgetDataHomes {
                all_stale: true,
                ..
            }]
        ),
        "{f:?}"
    );
}

#[test]
fn a_log_file_tails_with_the_webs_window_sizes() {
    let mut h = on_tab("Logs");
    click_row(&mut h, "gateway.log", "gateway.log");
    let s = h.turns(2);
    assert!(
        s.contains("from gateway-logs — newest lines at the bottom"),
        "{s}"
    );
    let reads = |h: &mut r8w4::Harness| -> Vec<u32> {
        sent_matching(h, |c| matches!(c, Cmd::LoadLogText { .. }))
            .into_iter()
            .filter_map(|c| match c {
                Cmd::LoadLogText { max_bytes, .. } => Some(max_bytes),
                _ => None,
            })
            .collect()
    };
    assert_eq!(reads(&mut h), vec![65536]);
    h.click_text("last 256 KB");
    assert_eq!(reads(&mut h), vec![262144]);
    click_on(&mut h, "last 1 MB", "↻");
    assert_eq!(reads(&mut h), vec![262144]);
    click_last(&mut h, "Close");
    let s = h.turns(2);
    assert!(!s.contains("newest lines at the bottom"), "{s}");
}

#[test]
fn hovering_a_button_shows_the_web_tooltip() {
    let mut h = chosen();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.starts_with(" run-live-1 ")
                .then(|| l.rfind("Cancel").map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("the Cancel button");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Cancel run run-live-1  (c)"), "{s}");
    // The head's ↻.
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.contains(" Runtimes")
                .then(|| l.rfind('↻').map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("↻");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Reload the runtime list  (r)"), "{s}");
}

#[test]
fn the_keyboard_reaches_every_control() {
    // Enter on the highlighted runtime chooses it.
    let mut h = page();
    h.key(b"\r");
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(
            &l[..],
            [Cmd::LoadRuns {
                scope: RunScope::Own,
                ..
            }]
        ),
        "{l:?}"
    );
    h.store.runs.set(Loadable::Ready(runs(0, false)));
    h.turns(2);
    let s = h.key(b"i");
    let s = if s.contains("Run run-live-1") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Run run-live-1"), "{s}");
    h.esc();
    let s = h.key(b"s");
    let s = if s.contains("Steer run") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Steer run"), "{s}");
    h.esc();
    let s = h.key(b"c");
    let s = if s.contains("Cancel run run-live-1?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Cancel run run-live-1?"), "{s}");
    h.esc();
    h.turns(2);
    h.key(b"t");
    h.turns(2);
    let l = sent_matching(&mut h, |c| matches!(c, Cmd::LoadRuns { .. }));
    assert!(
        matches!(
            &l[..],
            [Cmd::LoadRuns {
                root_only: false,
                ..
            }]
        ),
        "{l:?}"
    );
    let s = h.key(b"f");
    let s = if s.contains("cancelled") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("cancelled"), "f opens the status dropdown:\n{s}");
    h.esc();
    // w: the chosen runtime's Workspaces dialog.
    let s = h.key(b"w");
    let s = if s.contains("✕") { s } else { h.turns(2) };
    assert!(s.contains("Eligible workspaces") && s.contains("✕"), "{s}");
    // P / F on the Cache tab.
    let mut h = on_tab("Cache");
    h.key(b"P");
    dry_run_answers(&mut h, "prompt-kv", Some(1), Some(1));
    let s = h.turns(2);
    let s = if s.contains("Purge prompt-kv?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Purge prompt-kv?"), "{s}");
    h.esc();
    let s = h.key(b"F");
    let s = if s.contains("every stale registration") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("every stale registration"), "{s}");
    // o on Logs and Artifacts.
    let mut h = on_tab("Logs");
    let s = h.key(b"o");
    let s = if s.contains("from gateway-logs") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("from gateway-logs"), "{s}");
    let mut h = on_tab("Artifacts");
    let s = h.key(b"o");
    let s = if s.contains("text/markdown") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("text/markdown"), "{s}");
    // x drops the account filter.
    let mut h = page();
    h.store.runtime_filter.set(Some(RuntimeFilter {
        account: "alice".into(),
        tenant_id: "default".into(),
    }));
    h.turns(2);
    h.key(b"x");
    assert!(h.store.runtime_filter.get_untracked().is_none());
}

#[test]
fn every_tab_fits_80x24() {
    for tab in ["Runs", "Artifacts", "Cache", "Logs"] {
        let mut h = page_sized((80, 24));
        click_row(&mut h, "default", "default");
        h.store.runs.set(Loadable::Ready(runs(0, true)));
        h.turns(2);
        click_tab(&mut h, tab);
        h.store.artifacts.set(Loadable::Ready(artifacts()));
        h.store
            .data_homes
            .set(Loadable::Ready(data_homes_from_payload(&homes())));
        h.store
            .logs
            .set(Loadable::Ready(log_files_from_payload(&logs())));
        let s = h.turns(3);
        assert!(s.contains("▷ Runtime default"), "{tab}:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn a_non_admin_gets_the_reason() {
    let mut h = harness((100, 30), Mount::Page(page_view));
    h.identity("bob", false);
    let s = h.turns(3);
    assert!(s.contains("Runtimes"), "{s}");
    assert!(!s.contains("Workspace"), "{s}");
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
    assert_eq!(runtimes::TITLE, w("title"));
    assert_eq!(runtimes::SUBTITLE, w("subtitle"));
    assert_eq!(runtimes::NOTE, w("note"));
    assert_eq!(runtimes::RELOAD_TIP, w("reload_tip"));
    assert_eq!(runtimes::DETAIL_RELOAD_TIP, w("detail_reload_tip"));
    assert_eq!(runtimes::TEACH, w("teach"));
    assert_eq!(
        runtimes::TABS.to_vec(),
        vec![
            w("tab_runs"),
            w("tab_artifacts"),
            w("tab_cache"),
            w("tab_logs")
        ]
    );
    assert_eq!(runtimes::NO_RUNTIMES, w("no_runtimes"));
    assert_eq!(runtimes::STATUS_TIP, w("status_tip"));
    assert_eq!(runtimes::RUNS_SEARCH, w("runs_search"));
    assert_eq!(runtimes::ROOT_ONLY, w("root_only"));
    assert_eq!(runtimes::ROOT_ONLY_TIP, w("root_only_tip"));
    assert_eq!(runtimes::READONLY_NOTE, w("readonly_note"));
    assert_eq!(runtimes::STEER_TITLE, w("steer_title"));
    assert_eq!(runtimes::STEER_PLACEHOLDER, w("steer_placeholder"));
    assert_eq!(
        runtimes::steer_lead("R"),
        w("steer_lead").replace("{id}", "R")
    );
    assert_eq!(
        runtimes::cancel_question("R"),
        w("cancel_confirm").replace("{id}", "R")
    );
    assert_eq!(runtimes::MODALITY_TIP, w("modality_tip"));
    assert_eq!(runtimes::ARTIFACTS_SEARCH, w("artifacts_search"));
    assert_eq!(runtimes::ARTIFACTS_NOTE, w("artifacts_note"));
    assert_eq!(runtimes::CACHE_KIND_TIP, w("cache_kind_tip"));
    assert_eq!(runtimes::CACHES_SEARCH, w("caches_search"));
    assert_eq!(runtimes::CACHES_NOTE, w("caches_note"));
    assert_eq!(runtimes::PURGE_TIP, w("purge_tip"));
    let counts = PurgeCounts {
        files_deleted: Some(3),
        bytes_freed: Some(2048),
    };
    assert_eq!(
        runtimes::purge_question("N", &counts),
        format!(
            "{} {}",
            w("purge_title").replace("{n}", "N"),
            w("purge_confirm")
                .replace("{n}", "N")
                .replace("{files}", "3")
                .replace("{bytes}", "2.0 KiB")
        )
    );
    assert_eq!(runtimes::RETAINED_TIP, w("retained_note"));
    assert_eq!(runtimes::FORGET_TIP, w("forget_tip"));
    assert_eq!(runtimes::FORGET_ALL_TIP, w("forget_all_tip"));
    assert_eq!(
        runtimes::forget_question(Some("x")),
        w("forget_confirm").replace("{l}", "the stale row x")
    );
    assert_eq!(runtimes::LOGS_HOME_TIP, w("logs_home_tip"));
    assert_eq!(runtimes::LOGS_SEARCH, w("logs_search"));
    assert_eq!(runtimes::LOG_REFRESH_TIP, w("log_refresh_tip"));
    assert_eq!(
        runtimes::chip_clear_tip("a"),
        w("chip_clear_tip").replace("{a}", "a")
    );
    for (i, (_, l)) in runtimes::STATUSES.iter().enumerate() {
        assert_eq!(*l, w(&format!("status_{i}")));
    }
    for (i, (l, _)) in runtimes::TAIL_SIZES.iter().enumerate() {
        assert_eq!(*l, w(&format!("tail_{i}")));
    }
    // The buttons and links (label words; the glyph prefix is the web's icon).
    let r = &runs(0, false).rows[0];
    let acts = runtimes::run_actions(r, true);
    let labels: Vec<String> = acts.iter().map(|a| a.label.clone()).collect();
    assert_eq!(labels, vec![w("inspect"), w("steer"), w("cancel")]);
    assert_eq!(
        runtimes::cache_actions(false)[0].label,
        format!("× {}", w("purge"))
    );
    assert_eq!(
        runtimes::cache_actions(true)[0].label,
        format!("× {}", w("forget"))
    );
    assert!(runtimes::forget_all_action(3)
        .label
        .contains(&w("forget_all")));
    let inv = runtimes_from_payload(&inventory());
    assert_eq!(runtimes::inventory_actions(&inv[0])[0].label, w("eligible"));
    assert_eq!(
        runtimes::inventory_actions(&inv[1])[0].label,
        w("workspaces")
    );
    assert_eq!(runtimes::workspace_cell(&inv[2]), w("none"));
    // The columns, on screen.
    let mut h = chosen();
    let s = h.turns(1);
    for i in 0..6 {
        assert!(s.contains(&w(&format!("inv_col_{i}"))), "inv_col_{i}:\n{s}");
    }
    for i in 0..7 {
        assert!(
            s.contains(&w(&format!("runs_col_{i}"))),
            "runs_col_{i}:\n{s}"
        );
    }
    for (tab, prefix, n) in [
        ("Artifacts", "art_col", 6),
        ("Cache", "cache_col", 5),
        ("Logs", "log_col", 4),
    ] {
        let mut h = on_tab(tab);
        let s = h.turns(1);
        for i in 0..n {
            assert!(
                s.contains(&w(&format!("{prefix}_{i}"))),
                "{prefix}_{i}:\n{s}"
            );
        }
    }
    let mut h = chosen();
    h.store.runs.set(Loadable::Ready(runs(0, true)));
    let s = h.turns(2);
    assert!(s.contains(&w("prev")) && s.contains(&w("next")), "{s}");
}

#[test]
fn retained_runtimes_opens_the_reservations_dialog() {
    let mut h = page();
    click_on(&mut h, "Runtimes", "Retained runtimes");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::LoadReservations)));
    let s = h.turns(2);
    assert!(
        s.contains("Transfer one to a user — the data is never deleted."),
        "the Accounts dialog (a FormModal since f2b4a14):\n{s}"
    );
}
