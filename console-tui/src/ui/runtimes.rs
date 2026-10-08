//! Runtimes (R15, DESIGN-TUI §3.6 / web §8.4): the data-plane inventory
//! as a table whose row click CHOOSES the runtime, and under it the
//! chosen runtime's inspector — "▷ Runtime <id>" with the web's tabs
//! Runs | Artifacts | Cache | Logs, each a toolbar (the web's filter
//! dropdown + search field) over a table with the web's labelled row
//! buttons (Inspect / Steer / Cancel; Purge… / Forget). Inspect, Steer,
//! an artifact and a log tail are dialogs; Cancel, Purge and Forget ask
//! the web's question first.
//!
//! NOTHING below the inventory loads eagerly (operator directive
//! 2026-07-26): the inspector shows the web's teaching line until a
//! runtime is chosen, and each tab loads its own data on first look.
//! The runtime knobs that used to fold under this page live on the
//! pages that own them on the web (DESIGN D3): App settings, Skills'
//! shelf row, Workflows' defaults — their request bodies stay here.

use std::rc::Rc;

use abstracttui::app::select::{Select, SelectHandle};
use abstracttui::app::SelectOption;
use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use serde_json::{json, Value};

use super::util::{line, span};
use super::w::action::{button, On};
use super::w::form::sentence;
use super::w::{Action, Cell, Col, ColW, DataTable, Ink, Row as WRow, Segmented, Toggle};
use super::Ctx;
use crate::query::Needle;
use crate::store::{
    home_plane_index, human_bytes, ConnPhase, DataHomeRow, Loadable, RunRow, RunScope, RuntimeRow,
};
use crate::worker::Cmd;

// ------------------------------------------------------------ the web's words

pub const TITLE: &str = "Runtimes";
pub const SUBTITLE: &str = "Each user's own data plane: runs, flows, sessions and memory";
/// The web section note (what a runtime is, and how to open one).
pub const NOTE: &str = "A runtime is a user's own data plane: their runs, flows, sessions and memory. Each user gets one, named after them, unless an admin bound them to a shared one. Entities have their own too. Click a runtime to open its runs and cache below; the default runtime's cache also lists every machine-wide store.";
pub const RELOAD_TIP: &str = "Reload the runtime list";
pub const DETAIL_RELOAD_TIP: &str = "Reload this runtime's details";
pub const TEACH: &str = "Select a runtime above — click a row — to load its runs and cache.";
pub const TABS: [&str; 4] = ["Runs", "Artifacts", "Cache", "Logs"];
pub const NO_RUNTIMES: &str = "No runtimes found.";
pub const STATUS_TIP: &str = "Filter runs by their durable status";
pub const RUNS_SEARCH: &str = "Search runs — run id, workflow, session…";
pub const ROOT_ONLY: &str = "root runs only";
pub const ROOT_ONLY_TIP: &str = "Hide child runs — one row per top-level run";
pub const READONLY_NOTE: &str = "Read-only view — newest runs on this plane. Inspect/steer/cancel run through the default runtime's command lane and are not available here.";
pub const STEER_TITLE: &str = "Steer run";
pub const STEER_PLACEHOLDER: &str = "e.g. focus on the failing test first; prefer minimal diffs";
pub const MODALITY_TIP: &str = "Filter artifacts by type";
pub const ARTIFACTS_SEARCH: &str = "Search artifacts — name, kind, tags, date (YYYY-MM-DD)…";
pub const ARTIFACTS_NOTE: &str =
    "Artifacts are indexed on the gateway store this console session reads (all planes).";
pub const CACHE_KIND_TIP: &str = "Filter caches by kind";
pub const CACHES_SEARCH: &str = "Search caches — name, kind, path…";
pub const CACHES_NOTE: &str = "Disposable caches only — purging one just costs recomputation (re-download for model weights, re-encode for prompt KV). Durable stores are never listed here: deliverables live in the Artifacts tab, logs in the Logs tab.";
pub const PURGE_TIP: &str =
    "Delete the CONTENTS of this cache; a dry-run accounting is shown first";
pub const FORGET_TIP: &str = "Remove this stale registry row (disk untouched)";
pub const FORGET_ALL_TIP: &str = "Remove every stale registry row in one go (disk untouched)";
pub const LOGS_HOME_TIP: &str = "Filter by log home";
pub const LOGS_SEARCH: &str = "Search log files — file name…";
pub const LOG_REFRESH_TIP: &str = "Read the latest lines of this log again";
pub const TAIL_SIZES: [(&str, u32); 3] = [
    ("last 64 KB", 64 * 1024),
    ("last 256 KB", 256 * 1024),
    ("last 1 MB", 1024 * 1024),
];
pub const STATUSES: [(&str, &str); 6] = [
    ("", "all statuses"),
    ("running", "running"),
    ("waiting", "waiting"),
    ("completed", "completed"),
    ("failed", "failed"),
    ("cancelled", "cancelled"),
];

/// The web's Cancel question (`cancelRun`).
pub fn cancel_question(run_id: &str) -> String {
    format!("Cancel run {run_id}? Any in-flight work stops at the next tick.")
}

/// The web's Steer sentence (`steerRun`).
pub fn steer_lead(run_id: &str) -> String {
    format!(
        "Guidance folds into {run_id}'s next reasoning cycle (durable inbox — delivered at the loop boundary, never lost)."
    )
}

/// The web's Purge question (`purgeDataHome`): the dry-run's accounting
/// (a count the gateway did not report is said to be unknown, never 0).
pub fn purge_question(name: &str, counts: &crate::store::PurgeCounts) -> String {
    let files = match counts.files_deleted {
        Some(n) => format!("{n} files"),
        None => "an unknown number of files".to_string(),
    };
    let bytes = match counts.bytes_freed {
        Some(b) => human_bytes(b),
        None => "an unknown amount".to_string(),
    };
    format!(
        "Purge {name}? This deletes the CONTENTS of {name}: {files}, {bytes} freed. The directory itself and its registration survive. This cannot be undone."
    )
}

/// The Retained runtimes section's note (the web's), the head button's tooltip.
pub const RETAINED_TIP: &str = "Deleted or reassigned users leave their runtime data retained here — transfer it to a new owner or purge it permanently.";

/// The web's Forget question (`forgetDataHomes`): one row or all stale.
pub fn forget_question(name: Option<&str>) -> String {
    let label = match name {
        Some(n) => format!("the stale row {n}"),
        None => "every stale registration".to_string(),
    };
    format!(
        "This removes {label} from the data-home registry. Disk is never touched — the rows point at paths that no longer exist."
    )
}

/// The account chip's × tooltip (round 8).
pub fn chip_clear_tip(account: &str) -> String {
    format!("Show every runtime, not only {account}'s")
}

/// The inspector's sub-line under "▷ Runtime <id>" (the web's
/// `selectRuntime` bits).
pub fn detail_sentence(r: &RuntimeRow) -> String {
    let mut bits = vec![match r.kind.as_str() {
        "entity" => format!(
            "{} — the entity's own plane (visits, workflows, reflections run here)",
            if r.label.is_empty() {
                &r.runtime_id
            } else {
                &r.label
            }
        ),
        "user" => format!(
            "{} — this user's plane (their runs and flows live here)",
            r.label
        ),
        _ => "The gateway default runtime (admin plane)".to_string(),
    }];
    if r.kind == "entity" {
        bits.push(entity_state(r));
    }
    if let Some(n) = r.size_bytes {
        bits.push(human_bytes(n));
    }
    if r.data_dir.is_empty() {
        bits.push("not materialized yet".into());
    }
    format!("{}.", bits.join(" · "))
}

/// The State cell: entities carry state + liveness; other planes "—".
fn entity_state(r: &RuntimeRow) -> String {
    if r.liveness.as_deref() == Some("stopped") {
        return "STOPPED".into();
    }
    match r.state.as_deref() {
        None | Some("awake") | Some("") => "resting".into(),
        Some(s) => s.to_string(),
    }
}

/// The inventory's stable row key.
pub fn runtime_key(r: &RuntimeRow) -> String {
    format!("{}|{}|{}", r.kind, r.tenant_id, r.runtime_id)
}

// ------------------------------------------------------------ action sources

/// The page head's buttons: the account chip (when the page is filtered
/// to one account) and ↻.
pub fn head_actions(filter: Option<&crate::store::RuntimeFilter>) -> Vec<Action> {
    let mut out = Vec::new();
    if let Some(f) = filter {
        out.push(
            Action::label("account", format!("{} ×", f.chip()))
                .key('x')
                .tooltip(chip_clear_tip(&f.account)),
        );
    }
    out.push(Action::label("retained", "Retained runtimes").tooltip(RETAINED_TIP));
    out.push(Action::label("reload", "↻").key('r').tooltip(RELOAD_TIP));
    out
}

/// One inventory row's Workspace link (the web's Workspace cell), if any.
pub fn inventory_actions(r: &RuntimeRow) -> Vec<Action> {
    match r.kind.as_str() {
        "default" => vec![Action::link("workspaces", "Eligible workspaces")
            .key('w')
            .tooltip("Eligible workspaces of this gateway")],
        "user" | "entity" if r.owners.len() == 1 => vec![Action::link("workspaces", "Workspaces")
            .key('w')
            .tooltip(format!("Workspaces {}'s agents may use", r.owners[0]))],
        _ => Vec::new(),
    }
}

/// A run's buttons (the web's `loadRuns`): Inspect always; Steer and
/// Cancel while the run is live. A read-only plane (any runtime but the
/// default) offers none — its runs are ticked by that plane's own runtime.
pub fn run_actions(r: &RunRow, actionable: bool) -> Vec<Action> {
    if !actionable {
        return Vec::new();
    }
    let mut out = vec![Action::label("inspect", "Inspect")
        .key('i')
        .tooltip(format!("Show run {}", r.run_id))];
    if !matches!(r.status.as_str(), "completed" | "failed" | "cancelled") {
        out.push(
            Action::label("steer", "Steer")
                .key('s')
                .tooltip(format!("Send guidance to run {}", r.run_id)),
        );
        out.push(
            Action::label("cancel", "Cancel")
                .key('c')
                .tooltip(format!("Cancel run {}", r.run_id))
                .danger(),
        );
    }
    out
}

/// A cache row's button: Purge… on a live cache, Forget on a stale row.
pub fn cache_actions(stale: bool) -> Vec<Action> {
    if stale {
        vec![Action::label("forget", "× Forget").tooltip(FORGET_TIP)]
    } else {
        vec![Action::label("purge", "× Purge…")
            .key('P')
            .tooltip(PURGE_TIP)
            .danger()]
    }
}

/// The Cache tab's bulk button (the web shows it for two or more stale rows).
pub fn forget_all_action(n: usize) -> Action {
    Action::label("forget_all", format!("× Forget all stale ({n})"))
        .key('F')
        .tooltip(FORGET_ALL_TIP)
}

/// The inspector head's ↻.
pub fn detail_actions() -> Vec<Action> {
    vec![Action::label("reload_detail", "↻").tooltip(DETAIL_RELOAD_TIP)]
}

/// The footer's verbs.
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let store = ctx.store;
    if store.conn.with(ConnPhase::is_known_non_admin) {
        return Vec::new();
    }
    let mut out = vec![("↑↓ Enter", "open runtime"), ("w", "Workspaces")];
    if store.runtime_filter.with(Option::is_some) {
        out.push(("x", "every runtime"));
    }
    if ctx.ui.rt_detail.with(Option::is_some) {
        match ctx.ui.rt_tab.get() {
            0 => out.extend_from_slice(&[
                ("i", "Inspect"),
                ("s", "Steer"),
                ("c", "Cancel"),
                ("t", ROOT_ONLY),
                ("f", "status"),
                ("n/p", "page"),
            ]),
            1 => out.extend_from_slice(&[("o", "open"), ("f", "type"), ("n/p", "page")]),
            2 => {
                out.extend_from_slice(&[("P", "Purge…"), ("F", "Forget all stale"), ("f", "kind")])
            }
            _ => out.extend_from_slice(&[("o", "tail"), ("f", "log home")]),
        }
    }
    out.push(("r", "refresh"));
    out
}

// ------------------------------------------------------------ the page

/// The Runtimes screen is admin-only end to end: every read and write it
/// makes is an `/admin/*` route, and the web console hides the whole tab
/// for a non-admin. A principal known NOT to be an admin gets the reason
/// instead of a screen of 403 panels.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let non_admin = cx.memo(move || store.conn.with(ConnPhase::is_known_non_admin));
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::default().grow(1.0), move |scx| {
        if non_admin.get() {
            admin_only_view(scx, &tt, &ctx)
        } else {
            admin_view(scx, &ctx, &tt)
        }
    })
}

/// The Accounts Runtime jump (R8.2): the Runtimes page filtered to ONE
/// account — the web's `#runtimes?account=<id>`. The caller switches the
/// screen.
pub fn show_account(ctx: &Ctx, account: &str, tenant_id: &str) {
    let store = ctx.store;
    let filter = crate::store::RuntimeFilter {
        account: account.to_string(),
        tenant_id: tenant_id.to_string(),
    };
    store.runtime_filter.set(Some(filter.clone()));
    ctx.ui.rt_detail.set(None);
    ctx.ui.runtime_sel.set(0);
    store.runtimes.set(Loadable::Loading);
    ctx.send(Cmd::LoadRuntimesFor { filter });
}

/// The chip's ×: every runtime again.
fn clear_account_filter(ctx: &Ctx) {
    let store = ctx.store;
    if store.runtime_filter.get_untracked().is_none() {
        return;
    }
    store.runtime_filter.set(None);
    ctx.ui.rt_detail.set(None);
    ctx.ui.runtime_sel.set(0);
    store.runtimes.set(Loadable::Loading);
    ctx.send(Cmd::LoadRuntimes);
}

fn admin_only_view(cx: Scope, t: &TokenSet, ctx: &Ctx) -> View {
    let why = ctx
        .store
        .conn
        .with_untracked(|c| c.admin_refusal("the Runtimes screen"))
        .unwrap_or_default();
    let w = page_w(cx);
    Element::new()
        .style(LayoutStyle::column().grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .child(super::workflows::page_head(
            t,
            TITLE,
            SUBTITLE,
            w,
            Vec::new(),
        ))
        .child(sentence(t, &why, w, t.text_muted))
        .build()
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// Page-scoped state: the tables' keyed selections, the toolbar's
/// switch and the dropdowns' handles (f opens the active tab's).
#[derive(Clone)]
struct Pg {
    inv_key: Signal<Option<String>>,
    run_key: Signal<Option<String>>,
    art_key: Signal<Option<String>>,
    cache_key: Signal<Option<String>>,
    log_key: Signal<Option<String>>,
    root_only: Signal<bool>,
    picks: Rc<[SelectHandle; 4]>,
}

fn admin_view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let tt = *t;
    let pg = Pg {
        inv_key: cx.signal(None),
        run_key: cx.signal(None),
        art_key: cx.signal(None),
        cache_key: cx.signal(None),
        log_key: cx.signal(None),
        root_only: cx.signal(true),
        picks: Rc::new([
            SelectHandle::new(),
            SelectHandle::new(),
            SelectHandle::new(),
            SelectHandle::new(),
        ]),
    };
    install_effects(cx, ctx, &pg);

    let keys_ctx = ctx.clone();
    let keys_pg = pg.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .on(Phase::Bubble, move |ectx, ev| {
            if let UiEvent::Key(k) = ev {
                if k.mods.0 != 0 && !matches!(k.key, Key::Char(c) if c.is_ascii_uppercase()) {
                    return;
                }
                if handle_key(cx, &keys_ctx, &keys_pg, k.key) {
                    ectx.stop_propagation();
                }
            }
        })
        .child(head(cx, ctx, &tt))
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            // The web's note; a short terminal keeps its rows for the tables.
            let vp = crate::ui::page_viewport(cx).get();
            if vp.h < 30 {
                return Element::new().style(LayoutStyle::default().h(0)).build();
            }
            sentence(&tt, NOTE, (vp.w - 2).max(20), tt.text_muted)
        }))
        .child(inventory(cx, ctx, &tt, &pg))
        .child(inspector(cx, ctx, &tt, &pg))
        .build()
}

/// Loads (lazy, per chosen runtime and tab), the self-heal of the chosen
/// row, the table selections' sync with the legacy indices, and the
/// one-runtime account filter opening at once.
fn install_effects(cx: Scope, ctx: &Ctx, pg: &Pg) {
    let store = ctx.store;
    let ui = ctx.ui;
    // Each list clamps against ITS OWN row count.
    super::util::clamp_selection(cx, ui.run_sel, move || {
        store
            .runs
            .with(|d| d.ready().map(|d| d.rows.len()).unwrap_or(0))
    });
    super::util::clamp_selection(cx, ui.rt_art_sel, move || {
        store
            .artifacts
            .with(|d| d.ready().map(|a| a.rows.len()).unwrap_or(0))
    });
    super::util::clamp_selection(cx, ui.rt_logs_sel, move || {
        let home_f = ui.rt_logs_home.get();
        let query_f = ui.rt_logs_query.get();
        store.logs.with(|d| {
            d.ready()
                .map(|rows| filter_log_files(rows, &home_f, &query_f).len())
                .unwrap_or(0)
        })
    });
    {
        let ctx = ctx.clone();
        super::util::clamp_selection(cx, ui.home_sel, move || cache_rows(&ctx).0.len());
    }

    // The chosen runtime's runs (per-plane stores: following = switching
    // endpoints). Loading holds; Failed holds only for the scope last
    // asked (no retry loop); Ready holds when it answers this request.
    {
        let ctx_runs = ctx.clone();
        let root_only = pg.root_only;
        let last_requested = cx.signal(Option::<RunScope>::None);
        cx.effect(move || {
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            let Some(row) = ui.rt_detail.get() else {
                return;
            };
            // Self-heal against a reloaded inventory: a vanished plane
            // falls back to the teaching line; changed facts are adopted.
            let fresh = store.runtimes.with(|d| {
                d.ready().map(|rows| {
                    rows.iter()
                        .find(|r| runtime_key(r) == runtime_key(&row))
                        .cloned()
                })
            });
            match fresh {
                Some(None) => {
                    ui.rt_detail.set(None);
                    return;
                }
                Some(Some(f)) if f != row => {
                    ui.rt_detail.set(Some(f));
                    return;
                }
                _ => {}
            }
            let wanted = RunScope::of_runtime(&row);
            let status = ui.rt_runs_status.get();
            let query = ui.rt_runs_query.get();
            let offset = ui.rt_runs_offset.get();
            let want_root = matches!(wanted, RunScope::Own) && root_only.get();
            let held = store.runs.with(|d| match d {
                Loadable::Ready(r) => {
                    r.scope == wanted
                        && r.status == status
                        && r.query == query
                        && r.offset == offset
                        && (!matches!(wanted, RunScope::Own) || r.root_only == want_root)
                }
                Loadable::Loading => true,
                Loadable::Failed(_) => {
                    last_requested.with_untracked(|l| l.as_ref() == Some(&wanted))
                }
                Loadable::NotAsked => false,
            });
            if !held {
                if ui.run_sel.get_untracked() != 0 {
                    ui.run_sel.set(0);
                }
                last_requested.set(Some(wanted.clone()));
                store.runs.set(Loadable::Loading);
                ctx_runs.send(Cmd::LoadRuns {
                    scope: wanted,
                    status,
                    query,
                    offset,
                    root_only: want_root,
                });
            }
        });
    }
    // Artifacts (tab 1): one gateway-wide index, loaded on first look.
    {
        let ctx_art = ctx.clone();
        cx.effect(move || {
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            if ui.rt_tab.get() != 1 || ui.rt_detail.with(|d| d.is_none()) {
                return;
            }
            let modality = ui.rt_art_modality.get();
            let query = ui.rt_art_query.get();
            let offset = ui.rt_art_offset.get();
            let held = store.artifacts.with(|d| match d {
                Loadable::Ready(a) => {
                    a.modality == modality && a.query == query && a.offset == offset
                }
                Loadable::Loading => true,
                _ => false,
            });
            if !held {
                store.artifacts.set(Loadable::Loading);
                ctx_art.send(Cmd::LoadArtifacts {
                    offset,
                    modality,
                    query,
                });
            }
        });
    }
    // Cache (tab 2) and Logs (tab 3) read the data-homes registry — one
    // list, attributed to planes here; loaded on the first look.
    {
        let ctx_homes = ctx.clone();
        cx.effect(move || {
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            let tab = ui.rt_tab.get();
            if tab == 3
                && ui.rt_detail.with(|d| d.is_some())
                && matches!(store.logs.get(), Loadable::NotAsked)
            {
                store.logs.set(Loadable::Loading);
                ctx_homes.send(Cmd::LoadLogs);
            }
            if tab != 2 || ui.rt_detail.with(|d| d.is_none()) {
                return;
            }
            if matches!(store.data_homes.get(), Loadable::NotAsked) {
                store.data_homes.set(Loadable::Loading);
                // TWO-PHASE (web parity): the fast listing paints first;
                // the sized pass follows.
                ctx_homes.send(Cmd::LoadDataHomes { sizes: false });
                ctx_homes.send(Cmd::LoadDataHomes { sizes: true });
            }
        });
    }
    // The purge dry-run's answer: the web's question with its counts, or
    // the refusal (a refused dry-run vetoes the purge).
    {
        let ctx_p = ctx.clone();
        cx.effect(move || {
            let Some(plan) = store.purge_plan.get() else {
                return;
            };
            let mine = PURGE_PENDING.with(|p| p.borrow().as_deref() == Some(plan.name.as_str()));
            if !mine {
                return;
            }
            PURGE_PENDING.with(|p| *p.borrow_mut() = None);
            store.purge_plan.set(None);
            match plan.result {
                Ok(counts) => {
                    let c = ctx_p.clone();
                    let name = plan.name.clone();
                    super::w::Confirm::danger(
                        purge_question(&plan.name, &counts),
                        "Purge",
                        "Cancel",
                    )
                    .open(cx, ctx_p.ui, move || c.send(Cmd::PurgeDataHome { name }));
                }
                Err(why) => super::w::tip::say(&format!("Nothing purged: {why}")),
            }
        });
    }
    // R8.2: an account's filter that leaves exactly one runtime opens it.
    {
        let ctx_one = ctx.clone();
        cx.effect(move || {
            if store.runtime_filter.with(Option::is_none) || ui.rt_detail.with(Option::is_some) {
                return;
            }
            let one = store.runtimes.with(|d| {
                d.ready()
                    .and_then(|rows| (rows.len() == 1).then(|| runtime_key(&rows[0])))
            });
            if let Some(k) = one {
                choose(&ctx_one, &k);
            }
        });
    }
    // The inventory's highlighted row follows the chosen runtime (and a
    // click / Enter on a row chooses it).
    {
        let inv_key = pg.inv_key;
        cx.effect(move || {
            let want = ui.rt_detail.with(|d| d.as_ref().map(runtime_key));
            if inv_key.with_untracked(|k| *k != want) {
                inv_key.set(want);
            }
        });
        let ctx_c = ctx.clone();
        cx.effect(move || {
            if let Some(k) = inv_key.get() {
                let cur = ui.rt_detail.with_untracked(|d| d.as_ref().map(runtime_key));
                if cur.as_deref() != Some(k.as_str()) {
                    choose(&ctx_c, &k);
                }
            }
        });
    }
    // The tab tables' keys ↔ the legacy indices (kept: the clamps, the
    // keys and the tests read them).
    bridge(cx, pg.run_key, ui.run_sel, move || {
        store.runs.with(|d| {
            d.ready()
                .map(|d| d.rows.iter().map(|r| r.run_id.clone()).collect())
                .unwrap_or_default()
        })
    });
    bridge(cx, pg.art_key, ui.rt_art_sel, move || {
        store.artifacts.with(|d| {
            d.ready()
                .map(|a| (0..a.rows.len()).map(|i| i.to_string()).collect())
                .unwrap_or_default()
        })
    });
    bridge(cx, pg.log_key, ui.rt_logs_sel, move || {
        let home_f = ui.rt_logs_home.get();
        let query_f = ui.rt_logs_query.get();
        store.logs.with(|d| {
            d.ready()
                .map(|rows| {
                    filter_log_files(rows, &home_f, &query_f)
                        .iter()
                        .map(log_key)
                        .collect()
                })
                .unwrap_or_default()
        })
    });
    {
        let ctx_b = ctx.clone();
        bridge(cx, pg.cache_key, ui.home_sel, move || {
            let (live, stale) = cache_rows(&ctx_b);
            live.iter()
                .map(|(h, _)| format!("live:{}", h.name))
                .chain(stale.iter().map(|h| format!("stale:{}", h.name)))
                .collect()
        });
    }
}

/// Two-way sync of a table's keyed selection with an index signal.
fn bridge(
    cx: Scope,
    key: Signal<Option<String>>,
    idx: Signal<usize>,
    keys: impl Fn() -> Vec<String> + Clone + 'static,
) {
    let keys1 = keys.clone();
    cx.effect(move || {
        let i = idx.get();
        let ks = keys1();
        let want = ks.get(i).cloned();
        if want.is_some() && key.with_untracked(|k| *k != want) {
            key.set(want);
        }
    });
    cx.effect(move || {
        if let Some(k) = key.get() {
            let ks = abstracttui::reactive::untrack(&keys);
            if let Some(p) = ks.iter().position(|x| *x == k) {
                if idx.get_untracked() != p {
                    idx.set(p);
                }
            }
        }
    });
}

/// Choose a runtime to inspect (click / Enter on the inventory). Choosing
/// lands on Runs, like the web.
fn choose(ctx: &Ctx, key: &str) {
    let row = ctx.store.runtimes.with_untracked(|d| {
        d.ready()
            .and_then(|rows| rows.iter().find(|r| runtime_key(r) == key).cloned())
    });
    let Some(row) = row else { return };
    if let Some(i) = ctx.store.runtimes.with_untracked(|d| {
        d.ready()
            .and_then(|rows| rows.iter().position(|r| runtime_key(r) == key))
    }) {
        ctx.ui.runtime_sel.set(i);
    }
    ctx.ui.run_sel.set(0);
    ctx.ui.home_sel.set(0);
    ctx.ui.rt_tab.set(0);
    ctx.ui.rt_detail.set(Some(row));
}

/// The page keys (the footer lists them; every one is also a button).
fn handle_key(cx: Scope, ctx: &Ctx, pg: &Pg, key: Key) -> bool {
    let ui = ctx.ui;
    let chosen = ui.rt_detail.with_untracked(Option::is_some);
    let tab = ui.rt_tab.get_untracked();
    match key {
        Key::Char('x') => clear_account_filter(ctx),
        Key::Char('w') => open_workspaces(cx, ctx),
        Key::Char('/') => ctx.store.notice.set(Some(
            "the search field is in the toolbar — Tab reaches it, or click it".into(),
        )),
        _ if !chosen => return false,
        Key::Char('f') => {
            pg.picks[tab.min(3)].open();
        }
        Key::Char('n') => page_tab(ctx, 1),
        Key::Char('p') => page_tab(ctx, -1),
        Key::Char('t') if tab == 0 => {
            if selected_scope(ctx).is_some_and(|s| s.actionable()) {
                ui.rt_runs_offset.set(0);
                pg.root_only.update(|v| *v = !*v);
            }
        }
        Key::Char(c @ ('i' | 's' | 'c')) if tab == 0 => {
            let id = match c {
                'i' => "inspect",
                's' => "steer",
                _ => "cancel",
            };
            match selected_run(ctx) {
                Some((r, scope)) => match run_actions(&r, scope.actionable())
                    .into_iter()
                    .find(|a| a.id == id)
                {
                    Some(_) => run_action(cx, ctx, &r, id),
                    None if !scope.actionable() => super::w::tip::say(READONLY_NOTE),
                    None => super::w::tip::say(&format!(
                        "run {} is {} — nothing to {}",
                        r.run_id,
                        r.status,
                        if id == "steer" { "steer" } else { "cancel" }
                    )),
                },
                None => ctx.store.notice.set(Some("no run selected".into())),
            }
        }
        Key::Char('o') if tab == 1 => open_selected_artifact(cx, ctx),
        Key::Char('o') if tab == 3 => open_selected_log(cx, ctx),
        Key::Char('P') if tab == 2 => {
            let (live, _) = cache_rows(ctx);
            let key = pg.cache_key.get_untracked();
            let target = live
                .iter()
                .find(|(h, _)| key.as_deref() == Some(&format!("live:{}", h.name)))
                .or_else(|| live.first())
                .map(|(h, _)| h.clone());
            match target {
                Some(h) => confirm_purge(cx, ctx, h.name),
                None => ctx.store.notice.set(Some("no cache to purge".into())),
            }
        }
        Key::Char('F') if tab == 2 => {
            let (_, stale) = cache_rows(ctx);
            if stale.is_empty() {
                ctx.store.notice.set(Some("no stale registrations".into()));
            } else {
                confirm_forget(cx, ctx, None);
            }
        }
        _ => return false,
    }
    true
}

/// `n` / `p` and the pager's buttons — server-paged tabs only (Cache
/// and Logs list everything at once, like the web).
fn page_tab(ctx: &Ctx, dir: i32) {
    let ui = ctx.ui;
    let (offset, has_more) = match ui.rt_tab.get_untracked() {
        0 => (
            ui.rt_runs_offset,
            ctx.store
                .runs
                .with_untracked(|d| d.ready().map(|r| r.has_more).unwrap_or(false)),
        ),
        1 => (
            ui.rt_art_offset,
            ctx.store
                .artifacts
                .with_untracked(|d| d.ready().map(|a| a.has_more).unwrap_or(false)),
        ),
        _ => {
            ctx.store
                .notice
                .set(Some("this tab lists everything at once — no pages".into()));
            return;
        }
    };
    let cur = offset.get_untracked();
    if dir > 0 {
        if !has_more {
            ctx.store.notice.set(Some("last page".into()));
            return;
        }
        offset.set(cur + 100);
    } else {
        if cur == 0 {
            ctx.store.notice.set(Some("first page".into()));
            return;
        }
        offset.set(cur.saturating_sub(100));
    }
}

/// Title + subtitle; the account chip and ↻ on the right.
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let w = page_w(hcx);
        let filter = ctx.store.runtime_filter.get();
        let mut buttons = Vec::new();
        for a in head_actions(filter.as_ref()) {
            let c = ctx.clone();
            let wd = a.width();
            let id = a.id;
            buttons.push((
                button(hcx, &tt, &a, On::Page, true, move || match id {
                    "account" => clear_account_filter(&c),
                    "retained" => super::users::open_reservations_modal(pcx, &c),
                    _ => c.refresh_screen(super::SCREEN_RUNTIMES),
                }),
                wd,
            ));
        }
        super::workflows::page_head(&tt, TITLE, SUBTITLE, w, buttons)
    })
}

/// The inventory table: Runtime · Kind · Owner · State · Size ·
/// Workspace (a link to the Workspaces dialog). A click on a row chooses
/// the runtime (its runs and cache load below).
fn inventory(pcx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let inv_key = pg.inv_key;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
        let t = tt;
        let store = ctx.store;
        let vp = crate::ui::page_viewport(gcx).get();
        let w = (vp.w - 2).max(20);
        let data = store.runtimes.get();
        let rows_v: Vec<RuntimeRow> = data.ready().cloned().unwrap_or_default();
        let empty = match &data {
            Loadable::Loading | Loadable::NotAsked => "Scanning execution planes…".to_string(),
            Loadable::Failed(e) => format!("Runtime inventory unavailable: {}", e.message),
            Loadable::Ready(_) => match store.runtime_filter.get() {
                Some(f) => format!("{} owns no runtime yet.", f.account),
                None => NO_RUNTIMES.to_string(),
            },
        };
        let cols = vec![
            Col::new("Runtime", ColW::Fit { min: 8, max: 24 }),
            Col::new("Kind", ColW::Fit { min: 4, max: 8 }),
            Col::new("Owner", ColW::Flex { weight: 1, min: 8 }),
            Col::new("State", ColW::Fit { min: 5, max: 10 }),
            Col::new("Size", ColW::Fit { min: 4, max: 10 }),
            Col::new("Workspace", ColW::Fit { min: 9, max: 19 }),
        ];
        let rows: Vec<WRow> = rows_v
            .iter()
            .map(|r| {
                let mut owner = if r.owners.is_empty() {
                    if r.data_dir.is_empty() {
                        "(not materialized yet)".to_string()
                    } else {
                        String::new()
                    }
                } else {
                    r.owners.join(", ")
                };
                if let Some(n) = r.note.as_deref().filter(|n| !n.is_empty()) {
                    owner.push_str(&format!(" · {n}"));
                }
                let size = match r.size_bytes {
                    Some(n) if r.size_note.is_some() => format!("≥ {}", human_bytes(n)),
                    Some(n) => human_bytes(n),
                    None => String::new(),
                };
                let state = if r.kind == "entity" {
                    entity_state(r)
                } else {
                    "—".into()
                };
                let ws = match inventory_actions(r).into_iter().next() {
                    Some(a) => Cell::Link {
                        label: a.label.clone(),
                        action: a.id,
                        tip: a.tooltip.clone().map(|tp| format!("{tp}  (w)")),
                    },
                    None => Cell::text("None", t.text_faint),
                };
                WRow::new(
                    runtime_key(r),
                    vec![
                        Cell::text(r.runtime_id.clone(), t.text),
                        Cell::text(r.kind.clone(), t.text_muted),
                        Cell::text(owner, t.text_muted),
                        Cell::text(state, t.text_muted),
                        Cell::text(size, t.text_muted),
                        ws,
                    ],
                )
            })
            .collect();
        let max_rows = inventory_rows(rows.len(), vp.h) - 1;
        let (ca, ce, cs) = (ctx.clone(), ctx.clone(), ctx.clone());
        DataTable::new(cols, rows, inv_key)
            .width(w)
            .max_rows(max_rows.max(1))
            .empty(empty)
            .autofocus()
            .on_action(move |k, _id| {
                choose(&ca, k);
                open_workspaces(pcx, &ca);
            })
            .on_activate(move |k| choose(&ce, k))
            .on_space(move |k| choose(&cs, k))
            .view(gcx, &t)
    })
}

/// The inventory table's height: header + rows, at least 2 and at most
/// 40% of the terminal's height.
pub fn inventory_rows(n: usize, term_h: i32) -> i32 {
    let want = n as i32 + 1;
    want.clamp(2, (term_h * 2 / 5).max(2))
}

/// The chosen runtime's inspector: the teaching line, or "▷ Runtime
/// <id>" with the four tabs and ↻, the web's sub-line, and the active
/// tab's panel.
fn inspector(pcx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let pg = pg.clone();
    let ui = ctx.ui;
    dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |icx| {
        let t = tt;
        let w = page_w(icx);
        let rule = super::w::fill_line(
            LayoutStyle::line(1).shrink(0.0),
            vec![Ink::new("─".repeat(w.max(1) as usize), t.border)],
            None,
        );
        let Some(row) = ui.rt_detail.get() else {
            return Element::new()
                .style(LayoutStyle::column().shrink(0.0))
                .child(rule)
                .child(sentence(&t, TEACH, w, t.text_muted))
                .build();
        };
        let title = format!("▷ Runtime {}", row.runtime_id);
        let seg = Segmented::new(TABS, None)
            .bind(ui.rt_tab)
            .tip(0, "Runs")
            .tip(1, "Artifacts")
            .tip(2, "Cache")
            .tip(3, "Logs");
        let seg_w = seg.width();
        let mut reload = Vec::new();
        for a in detail_actions() {
            let c = ctx.clone();
            reload.push(button(icx, &t, &a, On::Page, true, move || {
                reload_detail(&c)
            }));
        }
        let head = Element::new()
            .style(
                LayoutStyle::row()
                    .gap(2)
                    .height(Dimension::Cells(1))
                    .shrink(0.0),
            )
            .child(super::w::fill_line(
                LayoutStyle::default()
                    .width(Dimension::Cells(abstracttui::text::width(&title)))
                    .height(Dimension::Cells(1)),
                vec![Ink::new(title.clone(), t.accent).bold()],
                None,
            ))
            .child(
                Element::new()
                    .style(LayoutStyle::default().width(Dimension::Cells(seg_w)).h(1))
                    .child(seg.view(icx, &t))
                    .build(),
            )
            .children(reload)
            .build();
        let vp = crate::ui::page_viewport(icx).get();
        let sub = if vp.h >= 30 {
            sentence(&t, &detail_sentence(&row), w, t.text_muted)
        } else {
            Element::new().style(LayoutStyle::default().h(0)).build()
        };
        let ctx_p = ctx.clone();
        let pg_p = pg.clone();
        let row_p = row.clone();
        let panel = dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |tcx| match ui
            .rt_tab
            .get()
        {
            0 => runs_panel(pcx, tcx, &ctx_p, &tt, &pg_p, &row_p),
            1 => artifacts_panel(pcx, tcx, &ctx_p, &tt, &pg_p),
            2 => cache_panel(pcx, tcx, &ctx_p, &tt, &pg_p),
            _ => logs_panel(pcx, tcx, &ctx_p, &tt, &pg_p),
        });
        Element::new()
            .style(LayoutStyle::column().gap(0).grow(1.0))
            .child(rule)
            .child(head)
            .child(sub)
            .child(panel)
            .build()
    })
}

/// The inspector's ↻: every tab's data reads again (each tab reloads on
/// its own next look).
fn reload_detail(ctx: &Ctx) {
    let s = ctx.store;
    s.runs.set(Loadable::NotAsked);
    s.artifacts.set(Loadable::NotAsked);
    s.data_homes.set(Loadable::NotAsked);
    s.logs.set(Loadable::NotAsked);
}

/// A toolbar: the tab's dropdown (with its web tooltip), its search field
/// and, on Runs, the root-runs switch. Two rows when the page is narrow.
#[allow(clippy::too_many_arguments)]
fn toolbar(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    handle: &SelectHandle,
    options: Vec<(String, String)>,
    current: Signal<String>,
    select_tip: &str,
    query: Signal<String>,
    placeholder: &str,
    live: bool,
    extra: Option<View>,
    on_change: impl Fn() + Clone + 'static,
) -> View {
    let w = page_w(cx);
    let values: Vec<String> = options.iter().map(|(v, _)| v.clone()).collect();
    let cur_ix = options
        .iter()
        .position(|(v, _)| *v == current.get_untracked())
        .unwrap_or(0);
    let ix = cx.signal(cur_ix);
    // The dropdown follows its filter when something else sets it (a
    // gateway reset clears the filters).
    {
        let vals = values.clone();
        cx.effect(move || {
            let cur = current.get();
            let want = vals.iter().position(|v| *v == cur).unwrap_or(0);
            if ix.get_untracked() != want {
                ix.set(want);
            }
        });
    }
    let on_sel = on_change.clone();
    let select = Select::new(
        options
            .iter()
            .map(|(_, l)| SelectOption::new(l.clone()))
            .collect(),
    )
    .value(ix)
    .handle(handle)
    .on_change(move |i| {
        if let Some(v) = values.get(i) {
            if current.get_untracked() != *v {
                current.set(v.clone());
                on_sel();
            }
        }
    })
    .layout(LayoutStyle::default().w(18).h(1).shrink(0.0))
    .element(cx, t);
    let select = super::w::tip::with_tip(cx, select, format!("{select_tip}  (f)")).build();
    let draft = cx.signal(query.get_untracked());
    cx.effect(move || {
        let q = query.get();
        if draft.with_untracked(|d| d.trim() != q) {
            draft.set(q);
        }
    });
    // The field takes what the dropdown and the switch leave (one row
    // down to 80 columns; the switch wraps under it below that).
    let extra_w = if extra.is_some() { 20 } else { 0 };
    let field_w = (w - 20 - extra_w).clamp(16, 44);
    let mut input = TextInput::new()
        .value(draft)
        .placeholder(placeholder.to_string())
        .layout(LayoutStyle::default().w(field_w).h(1).shrink(0.0));
    if live {
        input = input.on_change(move |s: &str| {
            let next = s.trim().to_string();
            if query.get_untracked() != next {
                query.set(next);
            }
        });
    } else {
        // ENTER COMMITS: the worker is one serial lane — a request per
        // keystroke would stampede it.
        input = input.on_submit(move |_| {
            let next = draft.get_untracked().trim().to_string();
            if query.get_untracked() != next {
                query.set(next);
                on_change();
            }
        });
    }
    let search = super::w::caret_tracked(cx, ctx.ui.caret, input.element(cx, t));
    let search = super::util::esc_releases_focus(search, ctx.store.notice).build();
    let narrow = extra.is_some() && 20 + field_w + extra_w > w;
    let mut row1 = Element::new()
        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
        .child(select)
        .child(search);
    if narrow {
        let mut col = Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .child(row1.build());
        if let Some(x) = extra {
            col = col.child(x);
        }
        return col.build();
    }
    if let Some(x) = extra {
        row1 = row1.child(x);
    }
    row1.build()
}

fn opts(list: &[(&str, &str)]) -> Vec<(String, String)> {
    list.iter()
        .map(|(v, l)| ((*v).to_string(), (*l).to_string()))
        .collect()
}

/// The web's pager: ‹ Prev · "1–100 · more" · Next › (nothing when one
/// page holds everything).
fn pager(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    offset: u32,
    shown: usize,
    has_more: bool,
    total: Option<u64>,
) -> View {
    if offset == 0 && !has_more {
        return Element::new().style(LayoutStyle::default().h(0)).build();
    }
    let (c1, c2) = (ctx.clone(), ctx.clone());
    let prev = Action::label("prev", "‹ Prev")
        .key('p')
        .refused((offset == 0).then(|| "first page".to_string()));
    let next = Action::label("next", "Next ›")
        .key('n')
        .refused((!has_more).then(|| "last page".to_string()));
    Element::new()
        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
        .child(button(cx, t, &prev, On::Page, true, move || {
            page_tab(&c1, -1)
        }))
        .child(line(vec![span(
            page_label(offset, shown, has_more, total),
            t.text_faint,
        )]))
        .child(button(cx, t, &next, On::Page, true, move || {
            page_tab(&c2, 1)
        }))
        .build()
}

/// The pager's position text (the web's `renderPager`).
fn page_label(offset: u32, shown: usize, has_more: bool, total: Option<u64>) -> String {
    let from = if shown > 0 {
        offset as usize + 1
    } else {
        offset as usize
    };
    let to = offset as usize + shown;
    match total {
        Some(t) if t as usize >= to => format!("{from}–{to} of {t}"),
        _ => format!("{from}–{to}{}", if has_more { " · more" } else { "" }),
    }
}

/// Lines `text` wraps to at the page width.
fn lines_of(text: &str, w: i32) -> i32 {
    super::util::wrap_text(text, w.max(10) as usize).len() as i32
}

/// How many table rows the active tab may use: the page height minus
/// what sits above the panel (head, note, inventory, inspector head and
/// sub-line) and the panel's own `chrome` (toolbar, table header,
/// pager / notes).
fn panel_rows(cx: Scope, ctx: &Ctx, chrome: i32) -> i32 {
    let vp = crate::ui::page_viewport(cx).get();
    let w = (vp.w - 2).max(20);
    let tall = vp.h >= 30;
    let n = ctx
        .store
        .runtimes
        .with(|d| d.ready().map(Vec::len).unwrap_or(0));
    let head = 1 + lines_of(SUBTITLE, w - 8);
    let note = if tall { lines_of(NOTE, w) } else { 0 };
    let inv = 2 + (inventory_rows(n, vp.h) - 1).max(1);
    let sub = if tall {
        ctx.ui
            .rt_detail
            .with(|d| d.as_ref().map(|r| lines_of(&detail_sentence(r), w)))
            .unwrap_or(0)
    } else {
        0
    };
    // One spare row: a status line under the page (and rounding in the
    // wrapped sentences) must never push the last control off screen.
    (vp.h - head - note - inv - 2 - sub - chrome - 1).max(2)
}

// ------------------------------------------------------------ Runs

fn runs_panel(pcx: Scope, cx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg, row: &RuntimeRow) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let w = page_w(cx);
    let actionable = RunScope::of_runtime(row).actionable();
    let mut col = Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
    if actionable {
        let root_only = pg.root_only;
        let toggle = Toggle::bound(root_only)
            .label(ROOT_ONLY)
            .tip(format!("{ROOT_ONLY_TIP}  (t)"))
            .on_change(move |v| {
                ui.rt_runs_offset.set(0);
                root_only.set(v);
            })
            .view(cx, &tt);
        col = col.child(toolbar(
            cx,
            ctx,
            &tt,
            &pg.picks[0],
            opts(&STATUSES),
            ui.rt_runs_status,
            STATUS_TIP,
            ui.rt_runs_query,
            RUNS_SEARCH,
            false,
            Some(toggle),
            move || ui.rt_runs_offset.set(0),
        ));
    } else {
        col = col.child(sentence(&tt, READONLY_NOTE, w, tt.text_muted));
    }
    let ctx_t = ctx.clone();
    let pg_t = pg.clone();
    col.child(dyn_view_scoped(
        LayoutStyle::column().gap(0).grow(1.0),
        move |gcx| {
            let data = store.runs.get();
            let t = tt;
            let w = page_w(gcx);
            let narrow = w < 100;
            let (rows_v, actionable_now, empty) = match &data {
                Loadable::Ready(d) => (
                    d.rows.clone(),
                    d.scope.actionable(),
                    runs_empty_text(&d.scope, &d.status, &d.query),
                ),
                Loadable::Failed(e) => (Vec::new(), false, e.message.clone()),
                _ => (Vec::new(), actionable, "Loading runs…".to_string()),
            };
            let mut cols = vec![
                Col::new("Run", ColW::Fit { min: 8, max: 36 }),
                Col::new("Workflow", ColW::Flex { weight: 1, min: 8 }),
                Col::new("Status", ColW::Fit { min: 6, max: 18 }),
            ];
            if !narrow && actionable_now {
                cols.push(Col::new("Node", ColW::Fit { min: 4, max: 14 }));
            }
            if !narrow {
                cols.push(Col::new("Session", ColW::Fit { min: 7, max: 14 }));
            }
            cols.push(Col::new("Updated", ColW::Fit { min: 7, max: 19 }));
            if actionable_now {
                cols.push(Col::new("Actions", ColW::Fit { min: 8, max: 26 }));
            }
            let rows: Vec<WRow> = rows_v
                .iter()
                .map(|r| {
                    let status = if r.paused {
                        format!("{} (paused)", r.status)
                    } else {
                        r.status.clone()
                    };
                    let ink = match r.status.as_str() {
                        "failed" => t.error,
                        "running" => t.ok,
                        _ => t.text_muted,
                    };
                    let mut cells = vec![
                        Cell::text(r.run_id.clone(), t.text),
                        Cell::text(r.workflow_id.clone(), t.text),
                        Cell::text(status, ink),
                    ];
                    if !narrow && actionable_now {
                        cells.push(Cell::text(r.current_node.clone(), t.text_muted));
                    }
                    if !narrow {
                        cells.push(Cell::text(r.session_id.clone(), t.text_muted));
                    }
                    // Narrow: "MM-DD HH:MM" (the year and seconds go first).
                    let updated: String = if narrow {
                        r.updated_at
                            .chars()
                            .skip(5)
                            .take(11)
                            .collect::<String>()
                            .replace('T', " ")
                    } else {
                        r.updated_at.chars().take(19).collect()
                    };
                    cells.push(Cell::text(updated, t.text_muted));
                    if actionable_now {
                        cells.push(Cell::Actions(run_actions(r, true)));
                    }
                    WRow::new(r.run_id.clone(), cells)
                })
                .collect();
            let (ca, ce) = (ctx_t.clone(), ctx_t.clone());
            let table = DataTable::new(cols, rows, pg_t.run_key)
                .width(w)
                .max_rows(panel_rows(gcx, &ctx_t, {
                    let bar = if actionable_now {
                        if w < 78 {
                            2
                        } else {
                            1
                        }
                    } else {
                        lines_of(READONLY_NOTE, w)
                    };
                    let paged = matches!(&data, Loadable::Ready(d) if d.offset > 0 || d.has_more);
                    bar + 2 + i32::from(paged)
                }))
                .empty(empty)
                .on_action(move |k, id| {
                    if let Some(r) = run_by_id(&ca, k) {
                        run_action(pcx, &ca, &r, id);
                    }
                })
                .on_activate(move |k| {
                    if let Some(r) = run_by_id(&ce, k) {
                        if actionable_now {
                            run_action(pcx, &ce, &r, "inspect");
                        }
                    }
                })
                .view(gcx, &t);
            let mut col = Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .child(table);
            if let Loadable::Ready(d) = &data {
                col = col.child(pager(
                    gcx,
                    &ctx_t,
                    &t,
                    d.offset,
                    d.rows.len(),
                    d.has_more,
                    None,
                ));
            }
            col.build()
        },
    ))
    .build()
}

fn run_by_id(ctx: &Ctx, id: &str) -> Option<RunRow> {
    ctx.store.runs.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.rows.iter().find(|r| r.run_id == id).cloned())
    })
}

/// The selected run + the scope its rows were loaded under (actions gate
/// on what is DISPLAYED).
fn selected_run(ctx: &Ctx) -> Option<(RunRow, RunScope)> {
    let idx = ctx.ui.run_sel.get_untracked();
    ctx.store.runs.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.rows.get(idx).cloned().map(|r| (r, d.scope.clone())))
    })
}

fn selected_scope(ctx: &Ctx) -> Option<RunScope> {
    ctx.ui
        .rt_detail
        .with_untracked(|d| d.as_ref().map(RunScope::of_runtime))
}

/// One run button (or its key).
fn run_action(cx: Scope, ctx: &Ctx, r: &RunRow, id: &str) {
    match id {
        "inspect" => open_run(cx, ctx, r.clone()),
        "steer" => open_steer(cx, ctx, r.clone()),
        "cancel" => {
            let c = ctx.clone();
            let run_id = r.run_id.clone();
            super::w::Confirm::danger(cancel_question(&r.run_id), "Cancel run", "Cancel").open(
                cx,
                ctx.ui,
                move || c.send(Cmd::CancelRun { run_id }),
            );
        }
        _ => {}
    }
}

/// Inspect: the web's run dialog (its rows; Close).
fn open_run(cx: Scope, ctx: &Ctx, r: RunRow) {
    let short: String = r.run_id.chars().take(12).collect();
    let lead = [r.workflow_id.as_str(), r.status.as_str()]
        .iter()
        .filter(|s| !s.is_empty())
        .cloned()
        .collect::<Vec<_>>()
        .join(" · ");
    let mut m = super::w::FormModal::new(format!("Run {short}")).size(96, 22);
    if !lead.is_empty() {
        m = m.lead(lead);
    }
    m.open(ctx, cx, move |mcx, close, _guard, inner_w| {
        let t = use_theme(mcx).get().tokens;
        let mut body = Element::new().style(LayoutStyle::column().shrink(0.0));
        for (k, v) in run_detail_rows(&r) {
            let lines = super::util::wrap_text(&v, (inner_w - 12).max(10) as usize);
            for (i, l) in lines.into_iter().enumerate() {
                let label = if i == 0 {
                    format!("{k:<10}  ")
                } else {
                    " ".repeat(12)
                };
                body = body.child(line(vec![span(label, t.text_muted), span(l, t.text)]));
            }
        }
        Element::new()
            .style(LayoutStyle::column().grow(1.0))
            .child(
                Scroll::new(body.build())
                    .layout(LayoutStyle::default().grow(1.0).min_h(3))
                    .element(mcx, &t)
                    .build(),
            )
            .child(super::w::form::button_row(vec![button(
                mcx,
                &t,
                &Action::label("close", "Close"),
                On::Raised,
                true,
                move || close(),
            )]))
            .build()
    });
}

/// Steer: the web's guidance dialog — Send guidance / Cancel, and
/// "Discard changes?" when typed guidance would be dropped.
fn open_steer(cx: Scope, ctx: &Ctx, r: RunRow) {
    let c = ctx.clone();
    super::w::FormModal::new(STEER_TITLE)
        .lead(steer_lead(&r.run_id))
        .size(84, 12)
        .open(ctx, cx, move |mcx, close, guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let guidance = mcx.signal(String::new());
            let esc_armed = mcx.signal(false);
            let form_error = mcx.signal(Option::<String>::None);
            super::install_dirty_guard(
                mcx,
                &guard,
                vec![(guidance, String::new())],
                esc_armed,
                form_error,
            );
            let send = {
                let c = c.clone();
                let close = close.clone();
                let run_id = r.run_id.clone();
                move || {
                    let g = guidance.get_untracked().trim().to_string();
                    if g.is_empty() {
                        form_error.set(Some("Type the guidance first.".into()));
                        return;
                    }
                    c.send(Cmd::SteerRun {
                        run_id: run_id.clone(),
                        guidance: g,
                    });
                    close();
                }
            };
            let send_enter = send.clone();
            let close_x = {
                let (close, guard) = (close.clone(), guard.clone());
                move || {
                    let handled = guard.borrow().as_ref().map(|g| g()).unwrap_or(false);
                    if !handled {
                        close();
                    }
                }
            };
            Element::new()
                .style(LayoutStyle::column().gap(0))
                .child(
                    super::w::caret_tracked(
                        mcx,
                        c.ui.caret,
                        TextInput::new()
                            .value(guidance)
                            .placeholder(STEER_PLACEHOLDER)
                            .on_submit(move |_: &str| send_enter())
                            .layout(LayoutStyle::default().w(inner_w.max(20)).h(1))
                            .element(mcx, &t),
                    )
                    .autofocus()
                    .build(),
                )
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    let t = abstracttui::app::current_theme().tokens;
                    match form_error.get() {
                        Some(e) => sentence(&t, &e, inner_w, t.error),
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                }))
                .child(super::w::fill_line(
                    LayoutStyle::line(1).shrink(0.0),
                    vec![],
                    None,
                ))
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t,
                        &Action::label("send", "Send guidance"),
                        On::Raised,
                        true,
                        send,
                    ),
                    button(
                        mcx,
                        &t,
                        &Action::label("close", "Cancel"),
                        On::Raised,
                        true,
                        close_x,
                    ),
                ]))
                .build()
        });
}

/// The web Runs table's empty sentence (`loadRuns`), or the read-only
/// plane's (`No runs on this runtime yet.`).
pub fn runs_empty_text(scope: &RunScope, status: &str, query: &str) -> String {
    if let RunScope::Plane { kind, .. } = scope {
        // The web's read-only plane sentence; an ENTITY plane also says
        // why empty is normal (operator 2026-07-26).
        return if kind == "entity" {
            "No runs on this runtime yet. Entity chats and life days don't create runtime runs; durable visits and summoned workflows land here.".to_string()
        } else {
            "No runs on this runtime yet.".to_string()
        };
    }
    match (status.is_empty(), query.is_empty()) {
        (_, false) if !status.is_empty() => format!("No {status} runs match \"{query}\"."),
        (_, false) => format!("No runs match \"{query}\"."),
        (false, true) => format!("No {status} runs."),
        (true, true) => "No runs yet.".to_string(),
    }
}

/// The web Inspect modal's rows for one run (`inspectRun`), in its
/// labels; empty values are left out.
pub fn run_detail_rows(r: &RunRow) -> Vec<(&'static str, String)> {
    [
        ("Run", r.run_id.clone()),
        ("Workflow", r.workflow_id.clone()),
        ("Status", r.status.clone()),
        ("Node", r.current_node.clone()),
        ("Session", r.session_id.clone()),
        ("Actor", r.actor_id.clone()),
        ("Waiting", r.waiting.clone()),
        ("Error", r.error.clone()),
        ("Created", r.created_at.chars().take(19).collect()),
        ("Updated", r.updated_at.chars().take(19).collect()),
        ("Parent", r.parent_run_id.clone().unwrap_or_default()),
    ]
    .into_iter()
    .filter(|(_, v)| !v.is_empty())
    .collect()
}

// ------------------------------------------------------------ Artifacts

fn artifacts_panel(pcx: Scope, cx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let ctx_t = ctx.clone();
    let pg_t = pg.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(toolbar(
            cx,
            ctx,
            &tt,
            &pg.picks[1],
            opts(&ARTIFACT_TYPES),
            ui.rt_art_modality,
            MODALITY_TIP,
            ui.rt_art_query,
            ARTIFACTS_SEARCH,
            false,
            None,
            move || ui.rt_art_offset.set(0),
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0),
            move |gcx| {
                let t = tt;
                let w = page_w(gcx);
                let narrow = w < 100;
                let data = store.artifacts.get();
                let empty = match &data {
                    Loadable::Ready(d) => {
                        let label = modality_label(&d.modality);
                        match (d.query.is_empty(), d.modality.is_empty()) {
                            (false, false) => format!("No {label} artifacts match \"{}\".", d.query),
                            (false, true) => format!("No artifacts match \"{}\".", d.query),
                            (true, false) => format!("No {label} artifacts yet."),
                            (true, true) => "No artifacts yet — runs that produce files, images, audio, or video will list them here.".to_string(),
                        }
                    }
                    Loadable::Failed(e) => format!("Artifacts unavailable: {}", e.message),
                    _ => "Loading artifacts…".to_string(),
                };
                let mut cols = vec![
                    Col::new("Artifact", ColW::Flex { weight: 2, min: 10 }),
                    Col::new("Type", ColW::Fit { min: 4, max: 8 }),
                    Col::new("Size", ColW::Fit { min: 4, max: 9 }),
                ];
                if !narrow {
                    cols.push(Col::new("Workflow", ColW::Flex { weight: 1, min: 8 }));
                    cols.push(Col::new("Run", ColW::Fit { min: 3, max: 12 }));
                }
                cols.push(Col::new("Created", ColW::Fit { min: 7, max: 19 }));
                let rows_v = data.ready().map(|d| d.rows.clone()).unwrap_or_default();
                let rows: Vec<WRow> = rows_v
                    .iter()
                    .enumerate()
                    .map(|(i, a)| {
                        let dash = |s: &str| if s.is_empty() { "—".to_string() } else { s.to_string() };
                        let mut cells = vec![
                            Cell::Link {
                                label: a.name.clone(),
                                action: "open",
                                tip: Some(format!("Click to preview {}  (o)", a.name)),
                            },
                            Cell::text(a.kind.clone(), t.text_muted),
                            Cell::text(a.size_bytes.map(human_bytes).unwrap_or_else(|| "—".into()), t.text_muted),
                        ];
                        if !narrow {
                            cells.push(Cell::text(dash(&a.workflow_id), t.text_muted));
                            cells.push(Cell::text(dash(&a.run_id.chars().take(12).collect::<String>()), t.text_muted));
                        }
                        cells.push(Cell::text(
                            a.created_at.replace('T', " ").chars().take(19).collect::<String>(),
                            t.text_muted,
                        ));
                        WRow::new(i.to_string(), cells)
                    })
                    .collect();
                let (ca, ce) = (ctx_t.clone(), ctx_t.clone());
                let (ra, re) = (rows_v.clone(), rows_v.clone());
                let table = DataTable::new(cols, rows, pg_t.art_key)
                    .width(w)
                    .max_rows(panel_rows(gcx, &ctx_t, {
                        let paged = matches!(&data, Loadable::Ready(d) if d.offset > 0 || d.has_more);
                        1 + 2 + i32::from(paged) + lines_of(ARTIFACTS_NOTE, w)
                    }))
                    .empty(empty)
                    .on_action(move |k, _| {
                        if let Some(a) = k.parse::<usize>().ok().and_then(|i| ra.get(i)) {
                            open_artifact(pcx, &ca, a.clone());
                        }
                    })
                    .on_activate(move |k| {
                        if let Some(a) = k.parse::<usize>().ok().and_then(|i| re.get(i)) {
                            open_artifact(pcx, &ce, a.clone());
                        }
                    })
                    .view(gcx, &t);
                let mut col = Element::new()
                    .style(LayoutStyle::column().gap(0).grow(1.0))
                    .child(table);
                if let Loadable::Ready(d) = &data {
                    col = col.child(pager(gcx, &ctx_t, &t, d.offset, d.rows.len(), d.has_more, Some(d.total)));
                    col = col.child(sentence(&t, ARTIFACTS_NOTE, w, t.text_faint));
                }
                col.build()
            },
        ))
        .build()
}

fn open_selected_artifact(cx: Scope, ctx: &Ctx) {
    let idx = ctx.ui.rt_art_sel.get_untracked();
    let row = ctx
        .store
        .artifacts
        .with_untracked(|d| d.ready().and_then(|a| a.rows.get(idx).cloned()));
    match row {
        Some(a) => open_artifact(cx, ctx, a),
        None => ctx.store.notice.set(Some("no artifact selected".into())),
    }
}

/// One artifact: its facts, and a preview — an image as a cell mosaic,
/// text in a scrolling pane; other kinds say plainly why not.
fn open_artifact(cx: Scope, ctx: &Ctx, a: crate::store::ArtifactRow) {
    // A previous preview must never paint under this artifact's header;
    // stamping the target lets the worker drop a late result.
    ctx.store.artifact_text.set(None);
    ctx.store.artifact_image.set(None);
    ctx.store
        .preview_target
        .set(crate::store::artifact_preview_key(
            &a.run_id,
            &a.artifact_id,
        ));
    let c = ctx.clone();
    let lead = format!(
        "{} · {} · {} · {}",
        a.kind,
        if a.content_type.is_empty() {
            "—"
        } else {
            &a.content_type
        },
        a.size_bytes.map(human_bytes).unwrap_or_else(|| "—".into()),
        a.created_at
            .replace('T', " ")
            .chars()
            .take(19)
            .collect::<String>()
    );
    // Two thirds of each axis (the previews scale with the terminal).
    let ps = super::preview_size(cx);
    super::w::FormModal::new(a.name.clone())
        .lead(lead)
        .size(ps.w, ps.h)
        .open(ctx, cx, move |mcx, close, _guard, inner_w| {
            let t = use_theme(mcx).get().tokens;
            let store = c.store;
            let preview = mcx.signal(String::new());
            let top = mcx.signal(0i32);
            {
                let a2 = a.clone();
                let c2 = c.clone();
                mcx.effect(move || {
                    if !preview.get_untracked().is_empty() {
                        return;
                    }
                    let textish = matches!(
                        a2.kind.as_str(),
                        "text" | "markdown" | "json" | "code" | "html"
                    );
                    let msg = if a2.run_id.is_empty() || a2.artifact_id.is_empty() {
                        "No run-scoped content route for this artifact (no run id) — metadata only.".to_string()
                    } else if a2.kind == "image" {
                        preview.set("decoding image…".to_string());
                        c2.send(Cmd::LoadArtifactImage {
                            run_id: a2.run_id.clone(),
                            artifact_id: a2.artifact_id.clone(),
                        });
                        return;
                    } else if !textish {
                        format!(
                            "{} artifacts do not render in a terminal — open this one in the web console's Artifacts tab.",
                            a2.kind
                        )
                    } else if a2.size_bytes.unwrap_or(0) > 256 * 1024 {
                        format!(
                            "Too large for a terminal preview ({}) — open it in the web console.",
                            a2.size_bytes.map(human_bytes).unwrap_or_default()
                        )
                    } else {
                        preview.set("loading preview…".to_string());
                        c2.send(Cmd::LoadArtifactText {
                            run_id: a2.run_id.clone(),
                            artifact_id: a2.artifact_id.clone(),
                        });
                        return;
                    };
                    preview.set(msg);
                });
            }
            mcx.effect(move || {
                if let Some(text) = store.artifact_text.get() {
                    preview.set(text);
                }
            });
            let dash = |s: &str| if s.is_empty() { "—".to_string() } else { s.to_string() };
            let facts = format!(
                "workflow: {} · run: {} · session: {}",
                dash(&a.workflow_id),
                dash(&a.run_id),
                dash(&a.session_id)
            );
            let path = format!(
                "path: {}",
                if a.content_path.is_empty() {
                    "— (served to admins only)".to_string()
                } else {
                    a.content_path.clone()
                }
            );
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(sentence(&t, &facts, inner_w, t.text_muted))
                .child(sentence(&t, &path, inner_w, t.text_muted))
                .child(dyn_view_scoped(
                    LayoutStyle::default().grow(1.0).min_h(3),
                    move |pcx| {
                        if let Some(bmp) = store.artifact_image.get() {
                            return Image::from_bitmap(bmp)
                                .fit(abstracttui::widgets::ImageFit::Contain)
                                .layout(LayoutStyle::default().grow(1.0))
                                .view(pcx);
                        }
                        let t = abstracttui::app::current_theme().tokens;
                        scroll_text_view(pcx, &t, preview.get(), top, None)
                    },
                ))
                .child(super::w::form::button_row(vec![button(
                    mcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || close(),
                )]))
                .build()
        });
}

// ------------------------------------------------------------ Cache

/// The Cache tab's rows for the chosen plane: (live caches after the
/// tab's filters, stale registrations). Stale rows are the whole
/// machine registry on the default plane (the web's).
fn cache_rows(ctx: &Ctx) -> (Vec<(DataHomeRow, bool)>, Vec<DataHomeRow>) {
    let ui = ctx.ui;
    let Some(row) = ui.rt_detail.get() else {
        return (Vec::new(), Vec::new());
    };
    let kind = ui.rt_cache_kind.get();
    let query = ui.rt_cache_query.get();
    let store = ctx.store;
    store.data_homes.with(|d| {
        let Some(homes) = d.ready() else {
            return (Vec::new(), Vec::new());
        };
        store.runtimes.with(|rt| {
            let planes = rt.ready().map_or(&[][..], |v| v.as_slice());
            let mine = displayed_homes(planes, homes, &row);
            let stale: Vec<DataHomeRow> = if row.kind == "default" {
                homes.iter().filter(|h| !h.exists).cloned().collect()
            } else {
                mine.iter()
                    .filter(|(h, _)| !h.exists)
                    .map(|(h, _)| h.clone())
                    .collect()
            };
            let live: Vec<(DataHomeRow, bool)> =
                mine.into_iter().filter(|(h, _)| h.exists).collect();
            (filter_homes(live, &kind, &query), stale)
        })
    })
}

fn cache_panel(pcx: Scope, cx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let mut kinds: Vec<String> = store.data_homes.with_untracked(|d| {
        d.ready()
            .map(|rows| {
                rows.iter()
                    .filter(|h| h.safe_to_purge && h.kind != "logs" && h.exists)
                    .map(|h| h.kind.clone())
                    .collect()
            })
            .unwrap_or_default()
    });
    kinds.sort();
    kinds.dedup();
    let mut options = vec![(String::new(), "all kinds".to_string())];
    options.extend(kinds.into_iter().map(|k| (k.clone(), k)));
    let ctx_t = ctx.clone();
    let pg_t = pg.clone();
    let w = page_w(cx);
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(toolbar(
            cx,
            ctx,
            &tt,
            &pg.picks[2],
            options,
            ui.rt_cache_kind,
            CACHE_KIND_TIP,
            ui.rt_cache_query,
            CACHES_SEARCH,
            true,
            None,
            move || ui.home_sel.set(0),
        ))
        .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
            let vp = crate::ui::page_viewport(cx).get();
            if vp.h < 30 {
                return Element::new().style(LayoutStyle::default().h(0)).build();
            }
            sentence(&tt, CACHES_NOTE, w, tt.text_faint)
        }))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0),
            move |gcx| {
                let t = tt;
                let w = page_w(gcx);
                let narrow = w < 100;
                let data = store.data_homes.get();
                let sizing = matches!(&data, Loadable::Ready(rows) if rows.iter().any(|h| h.exists && h.size_bytes.is_none()));
                let (live, stale) = cache_rows(&ctx_t);
                let kind = ui.rt_cache_kind.get();
                let query = ui.rt_cache_query.get();
                let empty = match &data {
                    Loadable::Ready(_) if !kind.is_empty() || !query.is_empty() => {
                        let mut bits = Vec::new();
                        if !kind.is_empty() {
                            bits.push(format!("kind \"{kind}\""));
                        }
                        if !query.is_empty() {
                            bits.push(format!("\"{query}\""));
                        }
                        format!("No caches match {}.", bits.join(" + "))
                    }
                    Loadable::Ready(_) => "No caches on this plane.".to_string(),
                    Loadable::Failed(e) => format!("Data homes unavailable: {}", e.message),
                    _ => "Loading caches…".to_string(),
                };
                let mut cols = vec![
                    Col::new("Cache", ColW::Flex { weight: 2, min: 12 }),
                    Col::new("Kind", ColW::Fit { min: 4, max: 12 }),
                    Col::new("Size", ColW::Fit { min: 4, max: 9 }),
                ];
                if !narrow {
                    cols.push(Col::new("Path", ColW::Flex { weight: 2, min: 12 }));
                }
                cols.push(Col::new("Actions", ColW::Fit { min: 8, max: 12 }));
                let mut rows: Vec<WRow> = Vec::new();
                for (h, _shared) in &live {
                    let size = match h.size_bytes {
                        Some(n) => human_bytes(n),
                        None if sizing => "…".into(),
                        None => "—".into(),
                    };
                    let mut cells = vec![
                        Cell::Lines(vec![
                            vec![Ink::new(h.name.clone(), t.text)],
                            vec![Ink::new(h.description.clone(), t.text_faint)],
                        ]),
                        Cell::text(h.kind.clone(), t.text_muted),
                        Cell::text(size, t.text_muted),
                    ];
                    if !narrow {
                        cells.push(Cell::text(h.path.clone(), t.text_muted));
                    }
                    cells.push(Cell::Actions(cache_actions(false)));
                    let mut r = WRow::new(format!("live:{}", h.name), cells);
                    if narrow {
                        r = r.note(Some((h.path.clone(), t.text_faint)));
                    }
                    rows.push(r);
                }
                let stale_head = format!(
                    "Stale registrations ({}, whole machine registry)",
                    stale.len()
                );
                for (i, h) in stale.iter().enumerate() {
                    let mut cells = vec![
                        Cell::text(h.name.clone(), t.text_muted),
                        Cell::text(h.kind.clone(), t.text_muted),
                        Cell::text("missing", t.text_muted),
                    ];
                    if !narrow {
                        cells.push(Cell::text(h.path.clone(), t.text_muted));
                    }
                    cells.push(Cell::Actions(cache_actions(true)));
                    let mut r = WRow::new(format!("stale:{}", h.name), cells).dim(true);
                    if i == 0 {
                        r = r.group(stale_head.clone());
                    }
                    rows.push(r);
                }
                let ca = ctx_t.clone();
                let table = DataTable::new(cols, rows, pg_t.cache_key)
                    .width(w)
                    .max_rows(panel_rows(gcx, &ctx_t, {
                        let note = if crate::ui::page_viewport(gcx).get_untracked().h >= 30 { lines_of(CACHES_NOTE, w) } else { 0 };
                        1 + note + 2 + 1 + i32::from(stale.len() > 1)
                    }))
                    .empty(empty)
                    .on_action(move |k, id| {
                        let name = k.split_once(':').map(|(_, n)| n.to_string()).unwrap_or_default();
                        match id {
                            "purge" => confirm_purge(pcx, &ca, name),
                            "forget" => confirm_forget(pcx, &ca, Some(name)),
                            _ => {}
                        }
                    })
                    .view(gcx, &t);
                let mut notes = Vec::new();
                if !sizing && !live.is_empty() {
                    let total: u64 = live.iter().filter_map(|(h, _)| h.size_bytes).sum();
                    notes.push(format!(
                        "{} cache{} · {} on disk.",
                        live.len(),
                        if live.len() == 1 { "" } else { "s" },
                        human_bytes(total)
                    ));
                }
                if sizing {
                    notes.push("measuring sizes — the list is complete, numbers are filling in…".into());
                }
                let mut col = Element::new()
                    .style(LayoutStyle::column().gap(0).grow(1.0))
                    .child(table);
                if !notes.is_empty() {
                    col = col.child(sentence(&t, &notes.join(" "), w, t.text_faint));
                }
                if stale.len() > 1 {
                    let c = ctx_t.clone();
                    col = col.child(super::w::form::button_row(vec![button(
                        gcx,
                        &t,
                        &forget_all_action(stale.len()),
                        On::Page,
                        true,
                        move || confirm_forget(pcx, &c, None),
                    )]));
                }
                col.build()
            },
        ))
        .build()
}

/// Purge…: the dry-run first (its accounting is the question), then the
/// web's confirm; the answer arrives in `store.purge_plan` (see
/// `install_effects`).
fn confirm_purge(_cx: Scope, ctx: &Ctx, name: String) {
    ctx.store.purge_plan.set(None);
    PURGE_PENDING.with(|p| *p.borrow_mut() = Some(name.clone()));
    ctx.send(Cmd::PurgeDryRun { name });
}

thread_local! {
    /// The cache whose purge dry-run is awaited.
    static PURGE_PENDING: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

fn confirm_forget(cx: Scope, ctx: &Ctx, name: Option<String>) {
    let c = ctx.clone();
    super::w::Confirm::plain(forget_question(name.as_deref()), "Forget", "Cancel").open(
        cx,
        ctx.ui,
        move || match name {
            Some(n) => c.send(Cmd::ForgetDataHomes {
                body: json!({ "name": n }).into(),
                all_stale: false,
            }),
            None => c.send(Cmd::ForgetDataHomes {
                body: json!({ "all_stale": true }).into(),
                all_stale: true,
            }),
        },
    );
}

// ------------------------------------------------------------ Logs

fn log_key(f: &crate::store::LogFileRow) -> String {
    format!("{}/{}", f.home, f.name)
}

fn logs_panel(pcx: Scope, cx: Scope, ctx: &Ctx, t: &TokenSet, pg: &Pg) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let mut homes: Vec<String> = store.logs.with_untracked(|d| {
        d.ready()
            .map(|rows| rows.iter().map(|f| f.home.clone()).collect())
            .unwrap_or_default()
    });
    homes.sort();
    homes.dedup();
    let mut options = vec![(String::new(), "all log homes".to_string())];
    options.extend(homes.into_iter().map(|h| (h.clone(), h)));
    let ctx_t = ctx.clone();
    let pg_t = pg.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0))
        .child(toolbar(
            cx,
            ctx,
            &tt,
            &pg.picks[3],
            options,
            ui.rt_logs_home,
            LOGS_HOME_TIP,
            ui.rt_logs_query,
            LOGS_SEARCH,
            true,
            None,
            move || ui.rt_logs_sel.set(0),
        ))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).grow(1.0),
            move |gcx| {
                let t = tt;
                let w = page_w(gcx);
                let data = store.logs.get();
                let home_f = ui.rt_logs_home.get();
                let query_f = ui.rt_logs_query.get();
                let all = data.ready().cloned().unwrap_or_default();
                let shown = filter_log_files(&all, &home_f, &query_f);
                let wants_default = ui
                    .rt_detail
                    .with(|d| d.as_ref().is_some_and(|r| r.kind == "default"));
                let empty = match &data {
                    Loadable::Ready(_) if !home_f.is_empty() || !query_f.is_empty() => {
                        let mut bits = Vec::new();
                        if !home_f.is_empty() {
                            bits.push(format!("home \"{home_f}\""));
                        }
                        if !query_f.is_empty() {
                            bits.push(format!("\"{query_f}\""));
                        }
                        format!("No log files match {}.", bits.join(" + "))
                    }
                    Loadable::Ready(_) if wants_default => "No log files yet.".to_string(),
                    Loadable::Ready(_) => "No log homes on this plane — serving logs live on the gateway default plane.".to_string(),
                    Loadable::Failed(e) => format!("Logs unavailable: {}", e.message),
                    _ => "Listing log files…".to_string(),
                };
                let cols = vec![
                    Col::new("File", ColW::Flex { weight: 2, min: 10 }),
                    Col::new("Log home", ColW::Flex { weight: 1, min: 8 }),
                    Col::new("Size", ColW::Fit { min: 4, max: 9 }),
                    Col::new("Modified", ColW::Fit { min: 8, max: 19 }),
                ];
                let rows: Vec<WRow> = shown
                    .iter()
                    .map(|f| {
                        WRow::new(
                            log_key(f),
                            vec![
                                Cell::Link {
                                    label: f.name.clone(),
                                    action: "tail",
                                    tip: Some(format!("Click to tail {}  (o)", f.name)),
                                },
                                Cell::text(f.home.clone(), t.text_muted),
                                Cell::text(f.size_bytes.map(human_bytes).unwrap_or_else(|| "—".into()), t.text_muted),
                                Cell::text(
                                    f.modified_at.replace('T', " ").chars().take(19).collect::<String>(),
                                    t.text_muted,
                                ),
                            ],
                        )
                    })
                    .collect();
                let (ca, ce) = (ctx_t.clone(), ctx_t.clone());
                let (sa, se) = (shown.clone(), shown.clone());
                let table = DataTable::new(cols, rows, pg_t.log_key)
                    .width(w)
                    .max_rows(panel_rows(gcx, &ctx_t, {
                        let notes = if shown.is_empty() { 0 } else { 2 };
                        1 + 2 + notes
                    }))
                    .empty(empty)
                    .on_action(move |k, _| {
                        if let Some(f) = sa.iter().find(|f| log_key(f) == k) {
                            open_log(pcx, &ca, f.home.clone(), f.name.clone());
                        }
                    })
                    .on_activate(move |k| {
                        if let Some(f) = se.iter().find(|f| log_key(f) == k) {
                            open_log(pcx, &ce, f.home.clone(), f.name.clone());
                        }
                    })
                    .view(gcx, &t);
                let mut notes = Vec::new();
                if shown.len() < all.len() {
                    let n = all.len() - shown.len();
                    notes.push(format!("{n} file{} hidden by the filter.", if n == 1 { "" } else { "s" }));
                }
                if !shown.is_empty() {
                    notes.push("Serving and launcher logs — regenerable text. Purge a log home from the CLI (abstractgateway data purge) if it grows too large.".to_string());
                }
                let mut col = Element::new()
                    .style(LayoutStyle::column().gap(0).grow(1.0))
                    .child(table);
                if !notes.is_empty() {
                    col = col.child(sentence(&t, &notes.join(" "), w, t.text_faint));
                }
                col.build()
            },
        ))
        .build()
}

fn open_selected_log(cx: Scope, ctx: &Ctx) {
    let idx = ctx.ui.rt_logs_sel.get_untracked();
    let home_f = ctx.ui.rt_logs_home.get_untracked();
    let query_f = ctx.ui.rt_logs_query.get_untracked();
    let row = ctx.store.logs.with_untracked(|d| {
        d.ready()
            .and_then(|rows| filter_log_files(rows, &home_f, &query_f).get(idx).cloned())
    });
    match row {
        Some(f) => open_log(cx, ctx, f.home, f.name),
        None => ctx.store.notice.set(Some("no log file selected".into())),
    }
}

/// Tail one log file: the web's log dialog — Show [last 64 KB | last
/// 256 KB | last 1 MB], ↻, the text, Close.
fn open_log(cx: Scope, ctx: &Ctx, home: String, file: String) {
    let read = {
        let c = ctx.clone();
        let (home, file) = (home.clone(), file.clone());
        move |max_bytes: u32| {
            c.store.log_text.set(None);
            c.store
                .preview_target
                .set(crate::store::log_preview_key(&home, &file));
            c.send(Cmd::LoadLogText {
                home: home.clone(),
                file: file.clone(),
                max_bytes,
            });
        }
    };
    read(TAIL_SIZES[0].1);
    let c = ctx.clone();
    // Two thirds of each axis (the previews scale with the terminal).
    let ps = super::preview_size(cx);
    super::w::FormModal::new(file.clone())
        .lead(format!("from {home} — newest lines at the bottom"))
        .size(ps.w, ps.h)
        .open(ctx, cx, move |mcx, close, _guard, _inner_w| {
            let t = use_theme(mcx).get().tokens;
            let store = c.store;
            let size = mcx.signal(0usize);
            let top = mcx.signal(0i32);
            let read_pick = read.clone();
            let seg = Segmented::new(TAIL_SIZES.iter().map(|(l, _)| *l), None)
                .bind(size)
                .on_pick(move |i| read_pick(TAIL_SIZES[i.min(2)].1));
            let read_again = read.clone();
            let again = button(
                mcx,
                &t,
                &Action::label("reread", "↻").tooltip(LOG_REFRESH_TIP),
                On::Raised,
                true,
                move || read_again(TAIL_SIZES[size.get_untracked().min(2)].1),
            );
            Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .child(
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        .child(line(vec![span("Show", t.text_muted)]))
                        .child(seg.view(mcx, &t))
                        .child(again)
                        .build(),
                )
                .child(dyn_view_scoped(
                    LayoutStyle::default().grow(1.0).min_h(3),
                    move |pcx| {
                        let t = abstracttui::app::current_theme().tokens;
                        let text = store
                            .log_text
                            .get()
                            .unwrap_or_else(|| "Reading tail…".to_string());
                        scroll_text_view(pcx, &t, text, top, None)
                    },
                ))
                .child(super::w::form::button_row(vec![button(
                    mcx,
                    &t,
                    &Action::label("close", "Close"),
                    On::Raised,
                    true,
                    move || close(),
                )]))
                .build()
        });
}

// ------------------------------------------------------------ Workspaces

/// The highlighted runtime's Workspaces dialog (the inventory's link).
fn open_workspaces(cx: Scope, ctx: &Ctx) {
    let row = ctx.ui.rt_detail.get_untracked().or_else(|| {
        let idx = ctx.ui.runtime_sel.get_untracked();
        ctx.store
            .runtimes
            .with_untracked(|d| d.ready().and_then(|rows| rows.get(idx).cloned()))
    });
    let Some(row) = row else {
        ctx.store
            .notice
            .set(Some("no runtime selected — no workspaces to show".into()));
        return;
    };
    match workspace_target(&row, &ctx.store) {
        Some(target) => super::workspace_chooser::open(cx, ctx, target),
        None => super::w::tip::say(&format!(
            "{}: no workspaces here (only the default plane and a one-owner user or entity plane have them)",
            row.runtime_id
        )),
    }
}

/// The inventory's Workspace cell (the web's link text).
pub fn workspace_cell(row: &crate::store::RuntimeRow) -> &'static str {
    match row.kind.as_str() {
        "default" => "Eligible workspaces",
        "user" | "entity" if row.owners.len() == 1 => "Workspaces",
        _ => "None",
    }
}

/// A scrollable read-only text pane. `CodeView` windows its own draw but
/// takes the offset FROM THE APP — nothing was driving it, so long logs
/// and JSON looked frozen at line 1 (operator 2026-08-19). Keys: ↑/↓ line,
/// PgUp/PgDn page, Home/End ends.
fn scroll_text_view(
    _cx: Scope,
    t: &TokenSet,
    text: String,
    top: Signal<i32>,
    lang: Option<&'static str>,
) -> View {
    let t0 = *t;
    let total = abstracttui::widgets::CodeView::line_count(&text) as i32;
    // The pane's own height is unknown until draw; a page is a sane 15
    // rows and the clamp keeps the last screen in view either way.
    let page = 15;
    let max_top = (total - 1).max(0);
    let cur = top.get().clamp(0, max_top);
    if cur != top.get_untracked() {
        top.set(cur);
    }
    let mut view = abstracttui::widgets::CodeView::new(text).scroll_offset(cur);
    if let Some(l) = lang {
        view = view.lang(l);
    }
    Element::new()
        .focusable()
        .autofocus()
        .style(LayoutStyle::column().grow(1.0))
        // MOUSE WHEEL (operator 2026-08-19): keys alone are not scrolling.
        // 3 lines per notch — the engine's own convention (file_picker,
        // list). stop_propagation so the wheel never also scrolls whatever
        // sits behind the modal.
        .on_event(move |ctx, ev| {
            let abstracttui::ui::UiEvent::Mouse(m) = ev else {
                return;
            };
            let delta = match m.kind {
                abstracttui::ui::MouseKind::ScrollUp => -3,
                abstracttui::ui::MouseKind::ScrollDown => 3,
                _ => return,
            };
            top.set((top.get_untracked() + delta).clamp(0, max_top));
            ctx.stop_propagation();
        })
        .shortcut(KeyChord::plain(Key::Down), move |_| {
            top.set((top.get_untracked() + 1).min(max_top));
        })
        .shortcut(KeyChord::plain(Key::Up), move |_| {
            top.set((top.get_untracked() - 1).max(0));
        })
        .shortcut(KeyChord::plain(Key::PageDown), move |_| {
            top.set((top.get_untracked() + page).min(max_top));
        })
        .shortcut(KeyChord::plain(Key::PageUp), move |_| {
            top.set((top.get_untracked() - page).max(0));
        })
        .shortcut(KeyChord::plain(Key::Home), move |_| top.set(0))
        .shortcut(KeyChord::plain(Key::End), move |_| top.set(max_top))
        .child(
            view.layout(LayoutStyle::default().grow(1.0))
                .element(&t0)
                .build(),
        )
        // Fixed row: the code pane grows, so a status line without its own
        // reserved height gets squeezed to nothing.
        .child(
            Element::new()
                .style(LayoutStyle::line(1).shrink(0.0))
                .child(line(vec![span(
                    format!(
                        "line {}/{}  —  ↑/↓ scroll · PgUp/PgDn page · Home/End ends",
                        cur + 1,
                        total.max(1)
                    ),
                    t0.text_faint,
                )]))
                .build(),
        )
        .build()
}

/// The body the apps form sends: only the settings whose text changed,
/// as flat `apps.<name>` keys; an emptied field sends "" (= clear back to
/// env/default), exactly like `abstractgateway apps config set <name> ""`.
pub fn apps_settings_body(
    current: &[crate::store::AppsSetting],
    typed: &[(String, String)],
) -> Value {
    let mut body = serde_json::Map::new();
    for (name, text) in typed {
        let Some(cur) = current.iter().find(|a| &a.name == name) else {
            continue;
        };
        let was = if cur.source == "stored" {
            cur.value.as_str()
        } else {
            ""
        };
        let now = text.trim();
        if now != was {
            body.insert(cur.key.clone(), Value::String(now.to_string()));
        }
    }
    Value::Object(body)
}

/// The body the default-agent form sends: only the interfaces whose text
/// changed, as {"agents": {"default_workflow": {interface: value}}}; an
/// emptied field sends "" (= back to the built-in default). `{}` when
/// nothing changed.
pub fn agent_defaults_body(
    current: &[crate::store::AgentDefault],
    typed: &[(String, String)],
) -> Value {
    let mut changed = serde_json::Map::new();
    for (iface, text) in typed {
        let Some(cur) = current.iter().find(|a| &a.interface == iface) else {
            continue;
        };
        let was = if cur.source == "stored" {
            cur.value.as_str()
        } else {
            ""
        };
        let now = text.trim();
        if now != was {
            changed.insert(iface.clone(), Value::String(now.to_string()));
        }
    }
    if changed.is_empty() {
        return Value::Object(serde_json::Map::new());
    }
    serde_json::json!({ "agents": { "default_workflow": Value::Object(changed) } })
}

/// The body the stream-replies form sends: `{"agents": {"streaming_default":
/// <bool>}}` when the switch changed, `{}` when it did not.
pub fn streaming_default_body(current: &crate::store::StreamingDefault, on: bool) -> Value {
    if on == current.value {
        return Value::Object(serde_json::Map::new());
    }
    serde_json::json!({ "agents": { "streaming_default": on } })
}

/// The body the skills shelf form sends: `{"skills.shelf": text}` when it
/// differs from the SAVED value ("" = back to the gateway's own copy); `{}`
/// when nothing changed.
pub fn skills_shelf_body(current: &crate::store::SkillsShelf, typed: &str) -> Value {
    let was = if current.source == "stored" {
        current.value.as_str()
    } else {
        ""
    };
    let now = typed.trim();
    if now == was {
        return Value::Object(serde_json::Map::new());
    }
    serde_json::json!({ "skills.shelf": now })
}

/// The Workspaces target of an inventory row (the web's Workspace cell):
/// default → the eligible workspaces; one owner → that account's.
pub fn workspace_target(
    row: &crate::store::RuntimeRow,
    store: &crate::store::Store,
) -> Option<super::workspace_chooser::Target> {
    use super::workspace_chooser::Target;
    match row.kind.as_str() {
        "default" => Some(Target::Gateway),
        "user" | "entity" if row.owners.len() == 1 => {
            let id = row.owners[0].clone();
            let tenant = if row.tenant_id.is_empty() {
                "default".to_string()
            } else {
                row.tenant_id.clone()
            };
            let own = store.conn.with_untracked(|c| match c {
                crate::store::ConnPhase::Connected(me) => {
                    me.user_id == id && me.tenant_id == tenant
                }
                _ => false,
            });
            Some(Target::Account {
                key: if own {
                    "me".into()
                } else {
                    format!("{tenant}:{id}")
                },
                id,
            })
        }
        _ => None,
    }
}

/// The type filter's options — the SAME comma modality lists the web
/// console sends (bare `audio` misses voice/music on the server's fast
/// path; a comma list forces the expanding post-filter).
const ARTIFACT_TYPES: [(&str, &str); 6] = [
    ("", "all types"),
    ("image", "image"),
    ("video", "video"),
    ("audio,voice,music,sound", "audio"),
    ("text,markdown,json,code,html", "text"),
    ("binary,document", "other"),
];

fn modality_label(value: &str) -> String {
    ARTIFACT_TYPES
        .iter()
        .find(|(v, _)| *v == value)
        .map(|(_, l)| (*l).to_string())
        .unwrap_or_else(|| value.to_string())
}

/// `query` is what the user TYPED — folding and wildcard classification
/// belong to [`Needle`], which does both once per call rather than once
/// per row.
fn filter_log_files(
    rows: &[crate::store::LogFileRow],
    home: &str,
    query: &str,
) -> Vec<crate::store::LogFileRow> {
    let needle = Needle::new(query);
    rows.iter()
        .filter(|f| home.is_empty() || f.home == home)
        .filter(|f| needle.matches(&f.name))
        .cloned()
        .collect()
}

/// The rows the Data tab displays for a plane: (home, shared) pairs —
/// shared=true only on the default plane (stores outside every plane).
/// `query` is what the user TYPED — see [`filter_log_files`].
fn filter_homes(
    rows: Vec<(DataHomeRow, bool)>,
    kind: &str,
    query: &str,
) -> Vec<(DataHomeRow, bool)> {
    let needle = Needle::new(query);
    rows.into_iter()
        .filter(|(h, _)| kind.is_empty() || h.kind == kind)
        .filter(|(h, _)| {
            needle.matches_any([
                h.name.as_str(),
                h.kind.as_str(),
                h.path.as_str(),
                h.description.as_str(),
            ])
        })
        .collect()
}

fn displayed_homes(
    planes: &[RuntimeRow],
    homes: &[DataHomeRow],
    row: &RuntimeRow,
) -> Vec<(DataHomeRow, bool)> {
    let sel_idx = planes.iter().position(|r| {
        r.kind == row.kind && r.tenant_id == row.tenant_id && r.runtime_id == row.runtime_id
    });
    let mut out = Vec::new();
    for h in homes {
        // A cache is a cache (operator 2026-08-19): only disposable,
        // non-log stores. STALE rows (path gone) stay — the Cache tab is
        // where the web console puts registry hygiene, with Forget.
        if !h.safe_to_purge || h.kind == "logs" {
            continue;
        }
        match home_plane_index(planes, &h.path) {
            Some(i) if Some(i) == sel_idx => out.push((h.clone(), false)),
            None if row.kind == "default" => out.push((h.clone(), true)),
            _ => {}
        }
    }
    // Plane-owned first, shared caches after — stable within groups.
    out.sort_by_key(|(_, shared)| *shared);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{DataHomeRow, LogFileRow};

    fn log(name: &str, home: &str) -> LogFileRow {
        LogFileRow {
            name: name.into(),
            home: home.into(),
            size_bytes: None,
            modified_at: String::new(),
        }
    }

    fn home(name: &str, kind: &str, path: &str) -> (DataHomeRow, bool) {
        (
            DataHomeRow {
                name: name.into(),
                path: path.into(),
                kind: kind.into(),
                owner: String::new(),
                safe_to_purge: true,
                description: String::new(),
                exists: true,
                size_bytes: None,
            },
            false,
        )
    }

    fn names(rows: &[LogFileRow]) -> Vec<&str> {
        rows.iter().map(|f| f.name.as_str()).collect()
    }

    #[test]
    fn logs_filter_reads_a_filetype_glob() {
        // The tab this was reported on: `*.jpg` used to match nothing at
        // all, because the `*` was compared literally.
        let rows = vec![
            log("gateway.log", "main"),
            log("gateway.log.1", "main"),
            log("screenshot.jpg", "runs"),
        ];
        assert_eq!(
            names(&filter_log_files(&rows, "", "*.log")),
            ["gateway.log"]
        );
        assert_eq!(
            names(&filter_log_files(&rows, "", "*.jpg")),
            ["screenshot.jpg"]
        );
        assert_eq!(
            names(&filter_log_files(&rows, "", "gateway.log*")),
            ["gateway.log", "gateway.log.1"]
        );
    }

    #[test]
    fn logs_filter_keeps_its_substring_half_and_its_home_filter() {
        let rows = vec![log("gateway.log", "main"), log("worker.log", "runs")];
        // No wildcard = the substring behaviour that shipped before.
        assert_eq!(names(&filter_log_files(&rows, "", "work")), ["worker.log"]);
        assert_eq!(names(&filter_log_files(&rows, "", "")).len(), 2);
        // The home dropdown and the query still AND together.
        assert_eq!(
            names(&filter_log_files(&rows, "main", "*.log")),
            ["gateway.log"]
        );
        assert!(filter_log_files(&rows, "main", "*.jpg").is_empty());
    }

    #[test]
    fn cache_filter_globs_across_the_fields_it_searches() {
        let rows = vec![
            home("runs", "runs", "/data/gw/runs"),
            home("hf-cache", "models", "/data/hf/hub"),
        ];
        let got = |kind: &str, q: &str| -> Vec<String> {
            filter_homes(rows.clone(), kind, q)
                .into_iter()
                .map(|(h, _)| h.name)
                .collect()
        };
        // A path glob — `*` crosses `/`, so this reaches into the value.
        assert_eq!(got("", "/data/hf/*"), ["hf-cache"]);
        // A name glob, and the basename pass over the stored path.
        assert_eq!(got("", "hf-*"), ["hf-cache"]);
        assert_eq!(got("", "hub"), ["hf-cache"]);
        // The kind dropdown still ANDs with the query.
        assert!(got("models", "runs").is_empty());
    }
}
