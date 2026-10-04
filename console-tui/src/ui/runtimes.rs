//! Runtimes: the data-plane inventory (default, per-user, per-entity)
//! with owners, sizes, entity liveness — plus, for the runtime the
//! operator CHOOSES, a tabbed inspector (Runs | Cache) and
//! the operational actions the web console carries: run cancel/steer
//! and data-home purge.
//!
//! NOTHING below the inventory loads eagerly (operator directive
//! 2026-07-26): the detail region shows a teaching line until a
//! runtime is chosen (click / Enter), and each tab loads its own data
//! lazily on first look.

use abstracttui::prelude::*;
use abstracttui::widgets::{Disclosure, SubmitPolicy, Table, Tabs, TextArea, TextAreaState};
use serde_json::{json, Value};

use super::util::{ellipsize, field, line, loadable_view, span, span_bold};
use super::widths;
use super::{open_form, Ctx};
use crate::query::Needle;
use crate::store::{
    home_plane_index, human_bytes, ConnPhase, DataHomeRow, Loadable, RunRow, RunScope, RunsData,
    RuntimeConfigData, RuntimeRow,
};
use crate::worker::Cmd;

/// The page note (DESIGN §7): what a runtime is, in the words every
/// surface uses — the block title carries the first sentence, the line
/// under the table the second.
pub const RUNTIME_TITLE: &str =
    "Runtimes — a runtime is a user's own data plane: their runs, flows, sessions and memory";
pub const RUNTIME_FOOTNOTE: &str = "Each user gets one, named after them, unless an admin bound them to a shared one  ·  w workspace policy";
/// The whole note, wrapped under the table when the terminal is too narrow
/// for the title to carry its first sentence.
pub const RUNTIME_NOTE: &str = "A runtime is a user's own data plane: their runs, flows, sessions and memory. Each user gets one, named after them, unless an admin bound them to a shared one. w workspace policy.";

/// The Runtimes screen is admin-only end to end: every read and write it
/// makes is an `/admin/*` route, and the web console hides the whole tab
/// for a non-admin (console.py renderAccount). A principal known NOT to
/// be an admin gets the reason instead of a screen of 403 panels; the
/// screen rebuilds when the principal changes.
pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let non_admin = cx.memo(move || store.conn.with(ConnPhase::is_known_non_admin));
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::default().grow(1.0), move |scx| {
        if non_admin.get() {
            admin_only_view(&tt, &ctx)
        } else {
            admin_view(scx, &ctx, &tt)
        }
    })
}

/// The Accounts Runtime jump (R8.2): the Runtimes page filtered to ONE
/// account — the web's `#runtimes?account=<id>` (same route, `GET
/// /admin/runtimes?account=`). The caller switches the screen.
pub fn show_account(ctx: &Ctx, account: &str, tenant_id: &str) {
    let store = ctx.store;
    let filter = crate::store::RuntimeFilter {
        account: account.to_string(),
        tenant_id: tenant_id.to_string(),
    };
    store.runtime_filter.set(Some(filter.clone()));
    // A chosen plane of another account would mislead under the filter.
    ctx.ui.rt_detail.set(None);
    ctx.ui.runtime_sel.set(0);
    store.runtimes.set(Loadable::Loading);
    ctx.send(Cmd::LoadRuntimesFor { filter });
}

/// `x`: every runtime again (the chip's ×).
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

fn admin_only_view(t: &TokenSet, ctx: &Ctx) -> View {
    let why = ctx
        .store
        .conn
        .with_untracked(|c| c.admin_refusal("the Runtimes screen"))
        .unwrap_or_default();
    Block::new()
        .border(BorderKind::Rounded)
        .title("Runtimes")
        .fill(t.surface)
        .layout(LayoutStyle::column().grow(1.0).padding(Edges::all(1)))
        .child(line(vec![span(why, t.text_muted)]))
        .child(line(vec![span(
            "data planes, runs, caches and the gateway knobs are admin surfaces — \
             sign in with an admin token to see them",
            t.text_faint,
        )]))
        .element(t)
        .build()
}

fn admin_view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, ui.runtime_sel, move || {
        store
            .runtimes
            .with(|d| d.ready().map(Vec::len).unwrap_or(0))
    });
    super::util::clamp_selection(cx, ui.run_sel, move || {
        store
            .runs
            .with(|d| d.ready().map(|d| d.rows.len()).unwrap_or(0))
    });
    // Each list tab clamps against ITS OWN row count. Sharing home_sel
    // (whose clamp is the cache row count) capped Artifacts and Logs at
    // the number of caches — the operator hit a hard stop on line 3.
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
    // F8-class for the Data tab, installed ONCE at page scope: purge
    // shrinks the displayed set — clamp the index. (It used to install
    // inside data_panel with the inspector's scope: every entry into
    // the Data tab re-ran the builder and stacked one more identical
    // effect for the inspector's lifetime.)
    super::util::clamp_selection(cx, ui.home_sel, move || {
        let Some(row) = ui.rt_detail.get() else {
            return 0;
        };
        store
            .data_homes
            .with(|d| {
                d.ready().map(|homes| {
                    store
                        .runtimes
                        .with(|rt| {
                            filter_homes(
                                displayed_homes(rt.ready().map_or(&[], |v| v), homes, &row),
                                &ui.rt_cache_kind.get(),
                                &ui.rt_cache_query.get(),
                            )
                        })
                        .len()
                })
            })
            .unwrap_or(0)
    });

    // Sessions follow the CHOSEN runtime (ui.rt_detail — set only by a
    // click/Enter on the inventory, never by mere highlight): runs
    // live in per-plane stores, so following = switching endpoints
    // (RunScope). Warm-keeper shape (the users screen's entity_detail
    // effect): Loading holds (no send storm — the tracked slot re-runs
    // this effect when a stale result lands and corrects to the
    // CURRENT choice); Failed holds only for the scope we last asked
    // (no retry loop against a persistently failing plane; recovery
    // stays r); Ready holds only when its scope matches the choice.
    // The web Runs toolbar's "root runs only" (checked by default) and
    // the run whose Inspect rows are open (Enter), both page-scoped; the
    // inline confirm for Cancel (the WUI's confirm, in place).
    let root_only = cx.signal(true);
    let run_expanded = cx.signal(Option::<usize>::None);
    let confirm = super::kit::InlineConfirm::new(cx);
    {
        let ctx_runs = ctx.clone();
        let last_requested = cx.signal(Option::<RunScope>::None);
        cx.effect(move || {
            if !store.conn.with(ConnPhase::is_connected) {
                return;
            }
            let Some(row) = ui.rt_detail.get() else {
                return; // nothing chosen — nothing loads
            };
            // Self-heal against a reloaded inventory: if the chosen
            // plane vanished (gateway switch, deleted user), fall back
            // to the teaching line instead of loading a dead plane; if
            // it still exists but its facts changed (size after r),
            // adopt the fresh row so the Data tab stays honest.
            let fresh = store.runtimes.with(|d| {
                d.ready().map(|rows| {
                    rows.iter()
                        .find(|r| {
                            r.kind == row.kind
                                && r.tenant_id == row.tenant_id
                                && r.runtime_id == row.runtime_id
                        })
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
                    return; // re-runs with the fresh row
                }
                _ => {}
            }
            let wanted = RunScope::of_runtime(&row);
            // The REQUEST is (plane, status, query, offset) — comparing the
            // plane alone left the toolbar dead: a filter change never
            // invalidated held data (design adversary Q4).
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
                // A different plane is a new context — the old row
                // index would silently target a different run.
                if ui.run_sel.get_untracked() != 0 {
                    ui.run_sel.set(0);
                }
                run_expanded.set(None);
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

    // Artifacts tab (index 1): deliverables metadata — load lazily on the
    // first look, reuse across planes (one gateway-wide index).
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

    // Cache tab (index 2) and Logs tab (index 3) both read the data-homes
    // registry — ONE global list (attributed to planes client-side); load
    // it lazily on the first look at either, then reuse across planes.
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
                // TWO-PHASE (web parity): the fast no-walk listing paints
                // first; the sized pass follows. A sized first call blocks
                // this SERIAL worker — cancels, steers and refreshes queue
                // behind a 30s tree walk.
                ctx_homes.send(Cmd::LoadDataHomes { sizes: false });
                ctx_homes.send(Cmd::LoadDataHomes { sizes: true });
            }
        });
    }

    // Runtime knobs (gateway-wide config): collapsed by default, load
    // on first expand.
    {
        let ctx_knobs = ctx.clone();
        cx.effect(move || {
            // Lazy stays LAW (2026-07-26 directive, pinned by
            // runtimes_screen_loads_nothing_eagerly): entering the screen
            // loads only the inventory. Discoverability comes from the
            // folded header's wording + the `w` hint instead — and `w`
            // itself self-heals by firing this load on first press.
            if !store.conn.with(ConnPhase::is_connected) || ui.rt_knobs_folded.get() {
                return;
            }
            if matches!(store.runtime_config.get(), Loadable::NotAsked) {
                store.runtime_config.set(Loadable::Loading);
                ctx_knobs.send(Cmd::LoadRuntimeConfig);
            }
        });
    }

    let ctx_cancel = ctx.clone();
    let ctx_steer = ctx.clone();
    let ctx_table = ctx.clone();
    let ctx_knobs = ctx.clone();
    let ctx_wsp = ctx.clone();
    let ctx_filter = ctx.clone();
    let ctx_search = ctx.clone();
    let ctx_next = ctx.clone();
    let ctx_prev = ctx.clone();
    let ctx_inspect = ctx.clone();
    let ctx_forget = ctx.clone();
    let ctx_open_row = ctx.clone();
    let ctx_clear = ctx.clone();
    // The inventory region regenerates when data lands (the runtimes
    // read, the lazy runtime-config read behind `w`): the keeper carries
    // the keyboard from one table instance to the next.
    let keeper = super::util::FocusKeeper::new();

    let page = Element::new()
        // gap 0: the bordered blocks separate themselves; at 80x24 the
        // two gap rows were exactly what starved the inventory table
        // (0240 class — see the min_h notes below).
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            cancel_selected(cx, &ctx_cancel, confirm);
        })
        // `t`: the web toolbar's "root runs only" (own plane, Runs tab).
        .shortcut(KeyChord::plain(Key::Char('t')), move |_| {
            if ui.rt_tab.get_untracked() == 0 && ui.rt_detail.with_untracked(|d| d.is_some()) {
                ui.rt_runs_offset.set(0);
                root_only.update(|v| *v = !*v);
            }
        })
        .shortcut(KeyChord::plain(Key::Char('s')), move |_| {
            steer_selected(cx, &ctx_steer);
        })
        .shortcut(KeyChord::plain(Key::Char('w')), move |_| {
            open_user_policy_for_selected(cx, &ctx_wsp);
        })
        .shortcut(KeyChord::plain(Key::Char('f')), move |_| {
            open_tab_filter(cx, &ctx_filter);
        })
        .shortcut(KeyChord::plain(Key::Char('/')), move |_| {
            open_tab_search(cx, &ctx_search);
        })
        .shortcut(KeyChord::plain(Key::Char('n')), move |_| {
            page_tab(&ctx_next, 1);
        })
        .shortcut(KeyChord::plain(Key::Char('p')), move |_| {
            page_tab(&ctx_prev, -1);
        })
        .shortcut(KeyChord::plain(Key::Char('i')), move |_| {
            inspect_selected_run(cx, &ctx_inspect);
        })
        // `o` opens the highlighted row of the ACTIVE tab. Enter does the
        // same when the table itself holds focus; the letter works from
        // anywhere on the screen (the tables are not always the focus).
        .shortcut(KeyChord::plain(Key::Char('o')), move |_| {
            open_selected_row(cx, &ctx_open_row);
        })
        // R8.2: `x` removes the account filter (the chip's ×).
        .shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            clear_account_filter(&ctx_clear);
        })
        .shortcut(KeyChord::plain(Key::Char('F')), move |_| {
            forget_stale_homes(cx, &ctx_forget);
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                // What a runtime is (DESIGN §7): the title says the first
                // sentence, the line under the table the second — no row
                // taken from the table.
                .title(RUNTIME_TITLE)
                .fill(t.surface)
                // min_h: the inventory is the screen's PRIMARY surface
                // (choosing happens here) — without a floor, the
                // inspector's own min crushed the whole table to ZERO
                // rows at 80x24 (border+padding+hint survived, every
                // runtime row gone; Enter then chose an invisible
                // row). 6 = border 2 + padding 2 + header 1 + ≥1 row.
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .shrink(1.0)
                        .min_h(6)
                        .padding(Edges::hv(1, 0)),
                )
                // R8.2: the account filter chip (the Accounts Runtime
                // jump); zero rows when every runtime is listed.
                .child(dyn_view(LayoutStyle::column().shrink(0.0), move || {
                    match store.runtime_filter.get() {
                        Some(f) => line(vec![
                            span_bold(format!("[{} ×]", f.chip()), tt.accent),
                            span("  x shows every runtime", tt.text_faint),
                        ]),
                        None => Element::new().style(LayoutStyle::default().h(0)).build(),
                    }
                }))
                .child(dyn_view_scoped(
                    LayoutStyle::default().shrink(1.0),
                    move |gcx| {
                        let data = store.runtimes.get();
                        let ctx_choose = ctx_table.clone();
                        // The per-user policy keys feed the table's policy
                        // column when the config has loaded (lazy law: the
                        // column teaches "press w" until then).
                        let policy_keys = match store.runtime_config.get() {
                            Loadable::Ready(cfg) => Some(
                                serde_json::from_str::<Value>(&cfg.user_workspace_policies)
                                    .ok()
                                    .and_then(|v| v.as_object().map(|m| m.keys().cloned().collect()))
                                    .unwrap_or_default(),
                            ),
                            _ => None,
                        };
                        super::util::loadable_view_kept(
                            &keeper,
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |d: &Vec<RuntimeRow>| d.is_empty(),
                            "no runtimes reported",
                            |d| {
                                table(gcx, &tt, d, ui.runtime_sel, policy_keys.clone(), &keeper, move |idx| choose(&ctx_choose, idx))
                            },
                        )
                    },
                ))
                .child({
                    let vp = abstracttui::app::use_viewport(cx);
                    dyn_view(LayoutStyle::column().gap(0).shrink(0.0), move || {
                        let w = vp.get().w;
                        if w >= 100 {
                            return line(vec![span(RUNTIME_FOOTNOTE, tt.text_faint)]);
                        }
                        // A short terminal keeps its rows for the runtimes
                        // themselves (the footer teaches `w`; About and the
                        // docs carry the sentence).
                        if vp.get().h < 30 {
                            return Element::new().style(LayoutStyle::default().h(0)).build();
                        }
                        let mut col =
                            Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                        for l in super::util::wrap_text(RUNTIME_NOTE, (w - 6).max(20) as usize) {
                            col = col.child(line(vec![span(l, tt.text_faint)]));
                        }
                        col.build()
                    })
                })
                .element(t)
                .build(),
        )
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Inspect — runs, artifacts, cache & logs of the chosen runtime")
                .fill(t.surface)
                // min_h: the sibling table's flex pressure must never
                // crush the inspector below a useful height (0240
                // class — the teaching line/tabs silently vanished).
                // 10, not 12: border 2 + padding 2 + label 1 + tabs 2 +
                // status 1 + runs header 1 + one run row — the exact
                // budget that still leaves the INVENTORY its own floor
                // at 80x24 (6 + 10 + knobs 1 = 17 ≤ the 18-row page).
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .min_h(10)
                        .padding(Edges::hv(1, 0)),
                )
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    let ctx_tabs = ctx.clone();
                    move |gcx| match ui.rt_detail.get() {
                        None => line(vec![span(
                            "select a runtime above — click a row, or Enter / double-click the highlighted one — to load its sessions and data",
                            tt.text_muted,
                        )]),
                        Some(row) => {
                            let ctx_s = ctx_tabs.clone();
                            let ctx_d = ctx_tabs.clone();
                            let ctx_a = ctx_tabs.clone();
                            let ctx_l = ctx_tabs.clone();
                            let row_s = row.clone();
                            let row_d = row.clone();
                            Element::new()
                                .style(LayoutStyle::column().gap(0))
                                .child(line(vec![
                                    span_bold(row.label.clone(), tt.accent),
                                    span(
                                        format!(
                                            "  {} plane · {}/{}",
                                            row.kind, row.tenant_id, row.runtime_id
                                        ),
                                        tt.text_faint,
                                    ),
                                ]))
                                .child(
                                    Tabs::new()
                                        // Panels receive the PAGE scope
                                        // (`cx`), not this dyn's
                                        // generation scope: modals/
                                        // prompts they open must
                                        // survive region re-renders
                                        // (a runs reload while the
                                        // steer form is open used to
                                        // orphan the form's signals —
                                        // typed guidance silently
                                        // dropped). Widgets inside the
                                        // panels still mount on their
                                        // own dyn scopes.
                                        // Operator 2026-08-19: tab order is
                                        // Runs | Artifacts | Cache — same
                                        // words in both consoles.
                                        .tab("Runs", move || {
                                            sessions_panel(
                                                cx,
                                                &ctx_s,
                                                &tt,
                                                &row_s,
                                                RunsPanelState {
                                                    root_only,
                                                    expanded: run_expanded,
                                                    confirm,
                                                },
                                            )
                                        })
                                        .tab("Artifacts", move || {
                                            artifacts_panel(cx, &ctx_a, &tt)
                                        })
                                        .tab("Cache", move || {
                                            data_panel(cx, &ctx_d, &tt, &row_d)
                                        })
                                        .tab("Logs", move || {
                                            logs_panel(cx, &ctx_l, &tt)
                                        })
                                        .active(ui.rt_tab)
                                        .layout(LayoutStyle::column().grow(1.0))
                                        .element(gcx, &tt)
                                        .build(),
                                )
                                .build()
                        }
                    }
                }))
                .element(t)
                .build(),
        )
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |dcx| {
                // The folded header carries the live posture summary so the
                // config surface is discoverable WITHOUT expanding (operator
                // 2026-08-19: a bare folded title read as "no config here").
                let knobs_title: String = match store.runtime_config.get() {
                    Loadable::Ready(d) => {
                        let count_lines = |s: &str| s.lines().filter(|l| !l.trim().is_empty()).count();
                        format!(
                            "Runtime knobs (gateway-wide) — {} · launch-folder trust {} · {} allowed · {} blocked · {} per-user",
                            if d.workspace_default_mode == "blacklist" { "allow-all posture" } else { "deny-all posture" },
                            if d.trust_client_launch_folder { "on" } else { "off" },
                            count_lines(&d.workspace_allowed_paths),
                            count_lines(&d.workspace_blocked_paths),
                            user_policy_count(&d),
                        )
                    }
                    _ => "Runtime knobs (gateway-wide) — workspace access policy, launch-folder trust, executors".to_string(),
                };
                Disclosure::new(knobs_title)
                    .folded(ui.rt_knobs_folded)
                    .max_body_rows(0)
                    .body({
                        let ctx_knobs = ctx_knobs.clone();
                        move |_bcx| {
                            let ctx_knobs = ctx_knobs.clone();
                        dyn_view(LayoutStyle::column().gap(0), move || {
                            let data = store.runtime_config.get();
                            loadable_view(
                                &tt,
                                &store.conn.get(),
                                || store.tick.get(),
                                &data,
                                |d: &RuntimeConfigData| {
                                    d.knobs.is_empty()
                                        && d.executors.is_empty()
                                        && d.workspace_root.is_empty()
                                        && d.workspace_allowed_paths.is_empty()
                                        && d.workspace_blocked_paths.is_empty()
                                        && d.apps.is_empty()
                                        && d.agent_defaults.is_empty()
                                        && d.skills_shelf.is_none()
                                        && d.streaming_default.is_none()
                                        && d.backlog.is_empty()
                                },
                                "the gateway reported no runtime knobs",
                                |d| knobs_view(cx, &ctx_knobs, &tt, d),
                            )
                        })
                    }})
                    .element(dcx, &tt)
                    .build()
            },
        ))
        ;
    confirm.keys(page).build()
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

/// `o` / Enter — open the highlighted row of the active tab: an artifact
/// detail (with a real image preview) or a log tail.
fn open_selected_row(cx: Scope, ctx: &Ctx) {
    match ctx.ui.rt_tab.get_untracked() {
        1 => {
            let idx = ctx.ui.rt_art_sel.get_untracked();
            let row = ctx
                .store
                .artifacts
                .with_untracked(|d| d.ready().and_then(|a| a.rows.get(idx).cloned()));
            match row {
                Some(a) => open_artifact_detail(cx, ctx, a),
                None => ctx.store.notice.set(Some("no artifact selected".into())),
            }
        }
        3 => {
            let idx = ctx.ui.rt_logs_sel.get_untracked();
            let home_f = ctx.ui.rt_logs_home.get_untracked();
            let query_f = ctx.ui.rt_logs_query.get_untracked();
            let row = ctx.store.logs.with_untracked(|d| {
                d.ready()
                    .and_then(|rows| filter_log_files(rows, &home_f, &query_f).get(idx).cloned())
            });
            match row {
                Some(f) => open_log_tail(cx, ctx, f.home, f.name),
                None => ctx.store.notice.set(Some("no log file selected".into())),
            }
        }
        _ => ctx.store.notice.set(Some(
            "nothing to open on this tab (i inspects a run)".into(),
        )),
    }
}

/// `f` — the active tab's filter dropdown (a ChoicePrompt: zero rows).
fn open_tab_filter(cx: Scope, ctx: &Ctx) {
    let ui = ctx.ui;
    if ui.rt_detail.with_untracked(|d| d.is_none()) {
        ctx.store.notice.set(Some("choose a runtime first".into()));
        return;
    }
    match ui.rt_tab.get_untracked() {
        0 => {
            let opts = [
                ("", "all statuses"),
                ("running", "running"),
                ("waiting", "waiting"),
                ("completed", "completed"),
                ("failed", "failed"),
                ("cancelled", "cancelled"),
            ];
            open_filter_prompt(
                cx,
                ctx,
                "Runs — status",
                &opts,
                ui.rt_runs_status,
                move || {
                    ui.rt_runs_offset.set(0);
                },
            );
        }
        1 => {
            let opts: Vec<(&str, &str)> = ARTIFACT_TYPES.to_vec();
            open_filter_prompt(
                cx,
                ctx,
                "Artifacts — type",
                &opts,
                ui.rt_art_modality,
                move || {
                    ui.rt_art_offset.set(0);
                },
            );
        }
        2 => {
            // Derived from the RENDERED rows, like the web console.
            let mut kinds: Vec<String> = ctx.store.data_homes.with_untracked(|d| {
                d.ready()
                    .map(|rows| {
                        rows.iter()
                            .filter(|h| h.safe_to_purge && h.kind != "logs")
                            .map(|h| h.kind.clone())
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default()
            });
            kinds.sort();
            kinds.dedup();
            let mut opts: Vec<(String, String)> = vec![(String::new(), "all kinds".into())];
            opts.extend(kinds.into_iter().map(|k| (k.clone(), k)));
            let borrowed: Vec<(&str, &str)> =
                opts.iter().map(|(v, l)| (v.as_str(), l.as_str())).collect();
            open_filter_prompt(cx, ctx, "Cache — kind", &borrowed, ui.rt_cache_kind, || {});
        }
        _ => {
            let mut homes: Vec<String> = ctx.store.logs.with_untracked(|d| {
                d.ready()
                    .map(|rows| rows.iter().map(|f| f.home.clone()).collect::<Vec<_>>())
                    .unwrap_or_default()
            });
            homes.sort();
            homes.dedup();
            let mut opts: Vec<(String, String)> = vec![(String::new(), "all log homes".into())];
            opts.extend(homes.into_iter().map(|h| (h.clone(), h)));
            let borrowed: Vec<(&str, &str)> =
                opts.iter().map(|(v, l)| (v.as_str(), l.as_str())).collect();
            open_filter_prompt(cx, ctx, "Logs — home", &borrowed, ui.rt_logs_home, || {});
        }
    }
}

/// `/` — the active tab's search, as a small modal. ENTER COMMITS (this
/// worker is a single serial lane; a keystroke-per-request search would
/// stampede it, and the web console's debounce+sequence dance exists only
/// because a browser can afford it).
fn open_tab_search(cx: Scope, ctx: &Ctx) {
    let ui = ctx.ui;
    if ui.rt_detail.with_untracked(|d| d.is_none()) {
        ctx.store.notice.set(Some("choose a runtime first".into()));
        return;
    }
    let tab = ui.rt_tab.get_untracked();
    let (title, target, offset): (&str, Signal<String>, Option<Signal<u32>>) = match tab {
        0 => (
            "Search runs — run id, workflow, session",
            ui.rt_runs_query,
            Some(ui.rt_runs_offset),
        ),
        1 => (
            "Search artifacts — name, kind, tags",
            ui.rt_art_query,
            Some(ui.rt_art_offset),
        ),
        2 => ("Search caches — name, kind, path", ui.rt_cache_query, None),
        _ => ("Search log files — file name", ui.rt_logs_query, None),
    };
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(74, 9), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let draft = mcx.signal(target.get_untracked());
        let close_ok = close.clone();
        let close_cancel = close.clone();
        let _ = &ctx2;
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(title.to_string(), t0.accent)]))
            .child(line(vec![span(
                "Enter applies · empty clears · Esc cancels",
                t0.text_faint,
            )]))
            // The glob half is stated ONCE, here, rather than crammed into
            // each tab's title: the rule is the same on all four.
            .child(line(vec![
                span("a ", t0.text_faint),
                span("*", t0.accent),
                span(" or ", t0.text_faint),
                span("?", t0.accent),
                span(" globs the whole value (", t0.text_faint),
                span("*.jpg", t0.accent),
                span("); plain text is a substring", t0.text_faint),
            ]))
            .child(field(
                &t0,
                "search",
                TextInput::new()
                    .value(draft)
                    .placeholder("type, then Enter")
                    .on_submit(move |_| {
                        target.set(draft.get_untracked().trim().to_string());
                        if let Some(off) = offset {
                            off.set(0);
                        }
                        close_ok();
                    })
                    .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(
                Button::new("Cancel (Esc)")
                    .on_click(move || close_cancel())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

/// `n` / `p` — page the active tab (server-paged tabs only; Cache and
/// Logs load whole, exactly like the web console, which shows no pager
/// there either).
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

/// `i` — inspect the highlighted run (the web console's run modal).
fn inspect_selected_run(cx: Scope, ctx: &Ctx) {
    let Some((row, _scope)) = selected_run(ctx) else {
        ctx.store.notice.set(Some("no run selected".into()));
        return;
    };
    let ctx2 = ctx.clone();
    let run_id = row.run_id.clone();
    open_form(ctx, cx, Size::new(92, 20), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let close_btn = close.clone();
        let _ = &ctx2;
        let mut rows: Vec<View> = Vec::new();
        let short: String = run_id.chars().take(12).collect();
        rows.push(line(vec![span_bold(format!("Run {short}"), t0.accent)]));
        // The web Inspect modal's rows (`inspectRun`), values wrapped.
        for (k, v) in run_detail_rows(&row) {
            for (i, l) in super::util::wrap_text(&v, 72).into_iter().enumerate() {
                let label = if i == 0 {
                    format!("{k:>10}: ")
                } else {
                    " ".repeat(12)
                };
                rows.push(line(vec![span(label, t0.text_muted), span(l, t0.text)]));
            }
        }
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .children(rows)
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_btn())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

/// `F` — forget every stale registry row (the web console's bulk
/// action). Disk is never touched; the gateway refuses live paths and
/// that refusal renders verbatim.
fn forget_stale_homes(cx: Scope, ctx: &Ctx) {
    if ctx.ui.rt_tab.get_untracked() != 2 {
        return;
    }
    let stale = ctx.store.data_homes.with_untracked(|d| {
        d.ready()
            .map(|rows| rows.iter().filter(|h| !h.exists).count())
            .unwrap_or(0)
    });
    if stale == 0 {
        ctx.store.notice.set(Some("no stale registrations".into()));
        return;
    }
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Forget {stale} stale registration(s)? Rows point at paths that no longer exist; disk is never touched."
        ),
        "Forget",
        "Keep",
        move || {
            ctx2.send(Cmd::ForgetDataHomes {
                body: json!({ "all_stale": true }).into(),
                all_stale: true,
            });
        },
    );
}

/// The VISIBLE toolbar row — a filter dropdown and a search box, the same
/// shape the web console wears. It costs one row, so it renders only when
/// the viewport can spare it (at 80x24 the inspector's own minimum already
/// owns every line — there the state rides the panel's status line and the
/// `f` / `/` gestures still work). Built on the PAGE scope: a Select's
/// popup dies with the scope that built it, and background store writes
/// regenerate the panel scopes constantly.
#[allow(clippy::too_many_arguments)]
fn toolbar_row(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    options: Vec<(String, String)>,
    current: Signal<String>,
    query: Signal<String>,
    placeholder: &'static str,
    on_change: impl Fn() + Clone + 'static,
) -> View {
    let t0 = *t;
    let ui = ctx.ui;
    let _ = ui;
    let values: Vec<String> = options.iter().map(|(v, _)| v.clone()).collect();
    let cur_ix = options
        .iter()
        .position(|(v, _)| *v == current.get_untracked())
        .unwrap_or(0);
    let ix = cx.signal(cur_ix);
    let sel_opts: Vec<abstracttui::app::SelectOption> = options
        .iter()
        .map(|(_, label)| abstracttui::app::SelectOption::new(label.clone()))
        .collect();
    let on_change_sel = on_change.clone();
    let values_sel = values.clone();
    let draft = cx.signal(query.get_untracked());
    Element::new()
        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
        .child(
            Select::new(sel_opts)
                .value(ix)
                .on_change(move |i| {
                    if let Some(v) = values_sel.get(i) {
                        if current.get_untracked() != *v {
                            current.set(v.clone());
                            on_change_sel();
                        }
                    }
                })
                .layout(LayoutStyle::default().w(26).h(1).shrink(0.0))
                .element(cx, &t0)
                .build(),
        )
        .child(
            // Esc hands the keyboard back to the panel (REVIEW-1 M2).
            super::util::esc_releases_focus(
                TextInput::new()
                    .value(draft)
                    .placeholder(placeholder)
                    // ENTER COMMITS: this worker is one serial lane, so a
                    // request per keystroke would stampede it.
                    .on_submit(move |_| {
                        let next = draft.get_untracked().trim().to_string();
                        if query.get_untracked() != next {
                            query.set(next);
                            on_change();
                        }
                    })
                    .layout(
                        LayoutStyle::default()
                            .basis(Dimension::Cells(0))
                            .grow(1.0)
                            .h(1),
                    )
                    .element(cx, &t0),
                ctx.store.notice,
            )
            .build(),
        )
        .build()
}

/// Does the viewport have a row to spare for the visible toolbar?
fn toolbar_fits(cx: Scope) -> bool {
    abstracttui::app::use_viewport(cx).get().h >= 28
}

/// The pager, as text (the terminal twin of the web console's
/// Prev/“X–Y of Z”/Next widget). `total` is None where the server has no
/// cheap count — then the line says "· more" instead of inventing one,
/// exactly like renderPager does.
fn page_label(offset: u32, shown: usize, has_more: bool, total: Option<u64>) -> String {
    if shown == 0 {
        return if offset == 0 {
            "0 rows".into()
        } else {
            format!("nothing at offset {offset}")
        };
    }
    let from = offset as usize + 1;
    let to = offset as usize + shown;
    match total {
        Some(t) if t as usize >= to => format!("{from}–{to} of {t}"),
        _ => format!("{from}–{to}{}", if has_more { " · more" } else { "" }),
    }
}

/// A filter dropdown, as a ChoicePrompt (zero rows — an inline Select
/// would cost the one spare row the 80x24 page has, and this app's
/// idiom is a bare letter opening a prompt).
fn open_filter_prompt(
    cx: Scope,
    ctx: &Ctx,
    title: &str,
    options: &[(&str, &str)],
    current: Signal<String>,
    on_pick: impl Fn() + 'static,
) {
    let mut prompt = abstracttui::app::ChoicePrompt::new(title.to_string());
    let cur = current.get_untracked();
    for (value, label) in options {
        prompt = prompt.option(*value, *label);
    }
    if options.iter().any(|(v, _)| *v == cur) {
        prompt = prompt.initial(cur.as_str());
    }
    let values: Vec<String> = options.iter().map(|(v, _)| v.to_string()).collect();
    super::open_prompt(cx, ctx.ui, prompt, move |outcome| {
        if let abstracttui::app::ChoiceOutcome::Answered(a) = outcome {
            if let Some(sel) = a.selected.first() {
                if values.iter().any(|v| v == sel) {
                    current.set(sel.clone());
                    on_pick();
                }
            }
        }
    });
}

/// Choose a runtime to inspect (click / Enter on the inventory). The
/// ONLY writer of `ui.rt_detail` besides the self-heal — nothing loads
/// before this runs.
fn choose(ctx: &Ctx, idx: usize) {
    let row = ctx
        .store
        .runtimes
        .with_untracked(|d| d.ready().and_then(|rows| rows.get(idx).cloned()));
    let Some(row) = row else { return };
    ctx.ui.run_sel.set(0);
    ctx.ui.home_sel.set(0);
    ctx.ui.rt_detail.set(Some(row));
}

/// Per-knob value + PROVENANCE (which layer set it: stored/env/default) —
/// the same honesty the web console's knob surface carries.
fn knobs_view(cx: Scope, ctx: &Ctx, t: &TokenSet, d: &RuntimeConfigData) -> View {
    let mut rows: Vec<View> = Vec::new();
    if !d.writable {
        rows.push(line(vec![
            span("writes: ", t.text_muted),
            span_bold("read-only (backend refuses writes)", t.warn),
        ]));
    }
    rows.push(line(vec![
        span(format!("{:>20}: ", "default_workspace"), t.text_muted),
        span(
            ellipsize(
                if d.workspace_root.trim().is_empty() {
                    "—"
                } else {
                    d.workspace_root.as_str()
                },
                48,
            ),
            t.text,
        ),
        span(format!("  ({})", d.workspace_root_source), t.text_faint),
    ]));
    rows.push(line(vec![
        span(format!("{:>20}: ", "allowed_workspaces"), t.text_muted),
        {
            let mounts_line = if d.workspace_allowed_paths.trim().is_empty() {
                "—".to_string()
            } else {
                d.workspace_allowed_paths.replace('\n', " · ")
            };
            span(ellipsize(&mounts_line, 48), t.text)
        },
        span(
            format!("  ({})", d.workspace_allowed_paths_source),
            t.text_faint,
        ),
    ]));
    rows.push(line(vec![
        span(format!("{:>20}: ", "blocked_workspaces"), t.text_muted),
        {
            let blocked_line = if d.workspace_blocked_paths.trim().is_empty() {
                "—".to_string()
            } else {
                d.workspace_blocked_paths.replace('\n', " · ")
            };
            span(ellipsize(&blocked_line, 48), t.text)
        },
        span(
            format!("  ({})", d.workspace_blocked_paths_source),
            t.text_faint,
        ),
    ]));
    rows.push(line(vec![
        span(format!("{:>20}: ", "default_posture"), t.text_muted),
        span(
            if d.workspace_default_mode == "blacklist" {
                "allow everything, refuse listed folders"
            } else {
                "deny everything, allow listed folders"
            },
            t.text,
        ),
        span(
            format!("  ({})", d.workspace_default_mode_source),
            t.text_faint,
        ),
    ]));
    rows.push(line(vec![
        span(format!("{:>20}: ", "launch_folder_trust"), t.text_muted),
        span(
            if d.trust_client_launch_folder {
                "true"
            } else {
                "false"
            },
            t.text,
        ),
        span(
            format!("  ({})", d.trust_client_launch_folder_source),
            t.text_faint,
        ),
        span("   scope_overrides: ", t.text_muted),
        span(
            if d.client_workspace_scope_overrides {
                "true"
            } else {
                "false"
            },
            t.text,
        ),
        span(
            format!("  ({})", d.client_workspace_scope_overrides_source),
            t.text_faint,
        ),
    ]));
    rows.push(line(vec![
        span(format!("{:>20}: ", "per_user_policies"), t.text_muted),
        {
            let count = user_policy_count(d);
            span(
                if count == 0 {
                    "— (press w on a user runtime row to customize)".to_string()
                } else {
                    format!("{count} account(s) customized — w on a user row edits")
                },
                t.text,
            )
        },
        span(
            format!("  ({})", d.user_workspace_policies_source),
            t.text_faint,
        ),
    ]));
    for (key, value, source) in &d.knobs {
        rows.push(line(vec![
            span(format!("{key:>20}: "), t.text_muted),
            span(ellipsize(value, 48), t.text),
            span(format!("  ({source})"), t.text_faint),
        ]));
    }
    if !d.executors.is_empty() {
        rows.push(line(vec![
            span(format!("{:>20}: ", "executors"), t.text_muted),
            span(d.executors.join(" · "), t.text),
        ]));
    }
    // Browser apps (mission Z): the apps.* settings with their source.
    for a in &d.apps {
        rows.push(line(vec![
            span(format!("{:>20}: ", a.key), t.text_muted),
            span(
                ellipsize(
                    if a.value.is_empty() {
                        "—"
                    } else {
                        a.value.as_str()
                    },
                    48,
                ),
                t.text,
            ),
            span(format!("  ({})", a.source), t.text_faint),
            span(format!("  {}", a.label), t.text_faint),
        ]));
        if !a.invalid.is_empty() {
            rows.push(line(vec![span(
                format!("{:>20}  ⚠ set aside: {}", "", a.invalid),
                t.warn,
            )]));
        }
    }
    // Default agent workflow per interface (agents.default_workflow): what
    // a client choosing "Gateway default" runs, or why it cannot.
    for a in &d.agent_defaults {
        let (now, tone) = if a.available {
            (format!("{} ({})", a.workflow_id, a.name), t.text)
        } else {
            (format!("unavailable: {}", a.reason), t.warn)
        };
        rows.push(line(vec![
            span(format!("{:>20}: ", "agent default"), t.text_muted),
            span(format!("{} → ", a.interface), t.text),
            span(ellipsize(&now, 72), tone),
            span(format!("  ({})", a.source), t.text_faint),
        ]));
    }
    // Stream replies by default (agents.streaming_default): on/off and its
    // source; a gateway whose read lacks the key says so (never hidden).
    match &d.streaming_default {
        Some(sd) => rows.push(line(vec![
            span(format!("{:>20}: ", "stream replies"), t.text_muted),
            span(
                if sd.value {
                    "on — interactive replies stream live"
                } else {
                    "off — replies arrive whole"
                },
                t.text,
            ),
            span(format!("  ({})", sd.source), t.text_faint),
        ])),
        None => rows.push(line(vec![
            span(format!("{:>20}: ", "stream replies"), t.text_muted),
            span(
                "not available on this gateway (its settings read has no agents.streaming_default)",
                t.warn,
            ),
        ])),
    }
    // The skills shelf (skills.shelf): which folder, from which source.
    if let Some(sh) = &d.skills_shelf {
        let (now, tone) = if sh.available {
            let v = if sh.bundled_version.is_empty() {
                String::new()
            } else {
                format!(" (curated {})", sh.bundled_version)
            };
            (format!("{}{v}", sh.resolved), t.text)
        } else {
            (format!("unavailable: {}", sh.reason), t.warn)
        };
        rows.push(line(vec![
            span(format!("{:>20}: ", "skills.shelf"), t.text_muted),
            span(ellipsize(&now, 80), tone),
            span(format!("  ({})", sh.source), t.text_faint),
        ]));
        for w in &sh.warnings {
            rows.push(line(vec![span(
                format!("{:>20}  ⚠ {}", "", ellipsize(w, 96)),
                t.warn,
            )]));
        }
    }
    // Backlog settings (Continuum): folder, exec runner, process manager —
    // value + where it comes from (web: "Advanced: backlog settings").
    for b in &d.backlog {
        let now = if b.redacted {
            "(hidden — admin only)".to_string()
        } else if b.value.is_empty() {
            "—".to_string()
        } else {
            b.value.clone()
        };
        rows.push(line(vec![
            span(format!("{:>20}: ", b.key), t.text_muted),
            span(ellipsize(&now, 72), t.text),
            span(
                format!(
                    "  ({})",
                    crate::store::operator::backlog_source_word(&b.source)
                ),
                t.text_faint,
            ),
        ]));
        if b.available == Some(false) {
            rows.push(line(vec![span(
                format!("{:>20}  ⚠ not available: {}", "", ellipsize(&b.reason, 90)),
                t.warn,
            )]));
        }
    }
    // The skills-shelf / backlog verbs get their OWN button row under the
    // first one (that row already overflows 160 columns; a row at the
    // bottom of the knobs falls off shorter terminals).
    let tail_row: View = if d.writable && (!d.backlog.is_empty() || d.skills_shelf.is_some()) {
        let ctx7 = ctx.clone();
        let current_backlog = d.clone();
        let ctx8 = ctx.clone();
        Element::new()
            .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
            .child(if current_backlog.backlog.is_empty() {
                Element::new().style(LayoutStyle::default().h(0)).build()
            } else {
                // Web: "Advanced: backlog settings" (Continuum).
                Button::new("Edit backlog settings")
                    .on_click(move || {
                        open_backlog_settings_form(cx, &ctx7, current_backlog.clone())
                    })
                    .element(cx, t)
                    .build()
            })
            .child(if d.skills_shelf.is_none() {
                Element::new().style(LayoutStyle::default().h(0)).build()
            } else {
                // Web: "Refresh the curated shelf" beside the shelf field.
                Button::new("Refresh the curated skills shelf")
                    .on_click(move || {
                        ctx8.send(Cmd::Operator(crate::worker::operator::OpCmd::ReseedSkills))
                    })
                    .element(cx, t)
                    .build()
            })
            .build()
    } else {
        Element::new().style(LayoutStyle::default().h(0)).build()
    };
    Element::new()
        .style(LayoutStyle::column())
        .child(if d.writable {
            let ctx5 = ctx.clone();
            let current_shelf = d.clone();
            let ctx2 = ctx.clone();
            let current = d.clone();
            let ctx3 = ctx.clone();
            let current_apps = d.clone();
            let ctx4 = ctx.clone();
            let current_agents = d.clone();
            let ctx6 = ctx.clone();
            let current_stream = d.clone();

            let row1 = Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(
                    Button::new("Edit workspace access policy")
                        .on_click(move || open_workspace_policy_form(cx, &ctx2, current.clone()))
                        .element(cx, t)
                        .build(),
                )
                .child(if current_apps.apps.is_empty() {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                } else {
                    Button::new("Edit apps settings")
                        .on_click(move || open_apps_settings_form(cx, &ctx3, current_apps.clone()))
                        .element(cx, t)
                        .build()
                })
                .child(if current_shelf.skills_shelf.is_none() {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                } else {
                    Button::new("Edit skills shelf")
                        .on_click(move || open_skills_shelf_form(cx, &ctx5, current_shelf.clone()))
                        .element(cx, t)
                        .build()
                })
                .build();
            // Second row: one row of five buttons ran past 80 columns.
            let row2 = Element::new()
                .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                .child(if current_agents.agent_defaults.is_empty() {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                } else {
                    Button::new("Edit default agent workflows")
                        .on_click(move || {
                            open_agent_defaults_form(cx, &ctx4, current_agents.clone())
                        })
                        .element(cx, t)
                        .build()
                })
                .child(if current_stream.streaming_default.is_none() {
                    Element::new().style(LayoutStyle::default().h(0)).build()
                } else {
                    Button::new("Edit stream replies")
                        .on_click(move || {
                            open_streaming_default_form(cx, &ctx6, current_stream.clone())
                        })
                        .element(cx, t)
                        .build()
                })
                .build();
            Element::new()
                .style(LayoutStyle::column().gap(0).shrink(0.0))
                .child(row1)
                .child(row2)
                .build()
        } else {
            Element::new().style(LayoutStyle::default().h(0)).build()
        })
        .child(tail_row)
        .children(rows)
        .build()
}

/// Backlog settings form (web "Advanced: backlog settings"): the folder
/// (prefilled with the SAVED value only — a default written back would
/// silently become a saved one), and two switches with a third "not
/// saved" state. Save sends only what changed; the gateway validates and
/// its sentence is shown on refusal. "Use the gateway's own folder"
/// fills the folder with the default path.
pub(crate) fn open_backlog_settings_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    use crate::store::operator::{backlog_settings_body, backlog_source_word};
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(110, 22), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let rows = current.backlog.clone();
        let fields: Vec<(String, Signal<String>)> = rows
            .iter()
            .map(|b| (b.key.clone(), mcx.signal(b.saved.clone())))
            .collect();
        let mut col = Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Backlog settings (Continuum)", t0.accent)]))
            .child(line(vec![span(
                "empty / not saved = the launch flag, else the default (the gateway's own folder; switches off) · applies at once",
                t0.text_faint,
            )]));
        for (b, (_, sig)) in rows.iter().zip(fields.iter()) {
            let sig = *sig;
            col = col.child(line(vec![
                span_bold(b.label.clone(), t0.text),
                span(
                    format!("  ({})", backlog_source_word(&b.source)),
                    t0.text_faint,
                ),
                span(
                    if b.flag.is_empty() {
                        String::new()
                    } else {
                        format!("  · launch flag: serve {}", b.flag)
                    },
                    t0.text_faint,
                ),
            ]));
            if b.is_folder() {
                col = col.child(field(
                    &t0,
                    "folder",
                    TextInput::new()
                        .value(sig)
                        .placeholder(if b.value.is_empty() {
                            b.default_path.clone()
                        } else {
                            b.value.clone()
                        })
                        .layout(LayoutStyle::default().w(80).h(1))
                        .element(mcx, &t0)
                        .build(),
                ));
                col = col.child(line(vec![span(
                    ellipsize(
                        &format!(
                            "in use: {}",
                            if b.value.is_empty() {
                                "(hidden)"
                            } else {
                                b.value.as_str()
                            }
                        ),
                        104,
                    ),
                    t0.text_faint,
                )]));
                if b.available == Some(false) {
                    col = col.child(line(vec![span(
                        ellipsize(&format!("⚠ not available: {}", b.reason), 104),
                        t0.warn,
                    )]));
                }
                if !b.default_path.is_empty() && b.value != b.default_path {
                    let def = b.default_path.clone();
                    let ctx_def = ctx2.clone();
                    col = col.child(
                        // Web parity: this button SAVES at once.
                        Button::new("Use the gateway's own folder")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_def.send(Cmd::SaveRuntimeConfig {
                                    body: json!({ "triage_repo_root": def.clone() }).into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    );
                }
            } else {
                let now = b.value.clone();
                col = col.child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    let label = match sig.get().as_str() {
                        "on" => "On".to_string(),
                        "off" => "Off".to_string(),
                        _ => format!("Not saved (now {now})"),
                    };
                    line(vec![
                        span(format!("{:>18} ", "saved:"), t0.text_muted),
                        span(label, t0.text),
                    ])
                }));
                col = col.child(
                    Button::new(format!("change {}", b.label.to_lowercase()))
                        .on_click(move || {
                            sig.update(|v| {
                                *v = match v.as_str() {
                                    "" => "on".to_string(),
                                    "on" => "off".to_string(),
                                    _ => String::new(),
                                }
                            })
                        })
                        .element(mcx, &t0)
                        .build(),
                );
            }
            col = col.child(line(vec![span(ellipsize(&b.help, 106), t0.text_faint)]));
        }
        let ctx_save = ctx2.clone();
        let close_cancel = close.clone();
        col.child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Save backlog settings")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let typed: Vec<(String, String)> = fields
                                    .iter()
                                    .map(|(k, sig)| (k.clone(), sig.get_untracked()))
                                    .collect();
                                let body = backlog_settings_body(&rows, &typed);
                                if body.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                                    form_error.set(Some("nothing changed".into()));
                                    return;
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_save.send(Cmd::SaveRuntimeConfig {
                                    body: body.into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
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

/// "Stream replies by default" dialog: one switch (agents.streaming_default),
/// applied at once.
fn open_streaming_default_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let Some(sd) = current.streaming_default.clone() else {
        return;
    };
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(100, 12), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        // A switch applies at once (no Save); the dialog stays open and
        // says the new state, a refusal reverts it with the reason.
        let on = mcx.signal(sd.value);
        let ok_note = mcx.signal(Option::<String>::None);
        let wanted = mcx.signal(sd.value);
        {
            let ui = ctx2.ui;
            mcx.effect(move || {
                if let Some((fid, outcome)) = ui.write_done.get() {
                    if fid == form_id {
                        ui.write_done.set(None);
                        in_flight.set(false);
                        match outcome {
                            Ok(_) => {
                                let w = wanted.get_untracked();
                                on.set(w);
                                form_error.set(None);
                                ok_note.set(Some(format!(
                                    "Stream replies is {}.",
                                    if w { "on" } else { "off" }
                                )));
                            }
                            Err(e) => {
                                ok_note.set(None);
                                form_error.set(Some(e));
                            }
                        }
                    }
                }
            });
        }
        let ctx_save = ctx2.clone();
        let close_cancel = close.clone();
        let sd_now = sd.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(sd.label.clone(), t0.accent)]))
            .child(line(vec![span(ellipsize(&sd.help, 96), t0.text_faint)]))
            .child(
                super::switch::Switch::new("Stream replies", on)
                    .busy_when(move || in_flight.get())
                    .on_request(move |want| {
                        if in_flight.get_untracked() {
                            return;
                        }
                        let current = crate::store::StreamingDefault {
                            value: on.get_untracked(),
                            ..sd_now.clone()
                        };
                        let body = streaming_default_body(&current, want);
                        if body.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                            return;
                        }
                        wanted.set(want);
                        form_error.set(None);
                        ok_note.set(None);
                        in_flight.set(true);
                        ctx_save.send(Cmd::SaveRuntimeConfig {
                            body: body.into(),
                            form_id: Some(form_id),
                        });
                    })
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            )
            .child(line(vec![span(
                "    interactive runs that do not ask either way stream their replies live",
                t0.text_faint,
            )]))
            .child(line(vec![span(
                format!("source before this dialog: {}", sd.source),
                t0.text_faint,
            )]))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let t = theme.get().tokens;
                match ok_note.get() {
                    Some(v) => line(vec![span(format!("✓ {v}"), t.ok)]),
                    None => line(vec![span("space switch · Esc close", t.text_faint)]),
                }
            }))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_cancel())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

/// Default agent workflow form: one line per agent interface, prefilled
/// with the SAVED value only (the built-in default is shown, never written
/// back); the choices on this gateway are listed under each line; the
/// gateway validates (the workflow must declare the interface) and its
/// sentence is shown on refusal.
fn open_agent_defaults_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let ctx2 = ctx.clone();
    let height = 12 + 3 * current.agent_defaults.len() as i32;
    open_form(ctx, cx, Size::new(110, height), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let fields: Vec<(String, Signal<String>)> = current
            .agent_defaults
            .iter()
            .map(|a| {
                (
                    a.interface.clone(),
                    mcx.signal(if a.source == "stored" {
                        a.value.clone()
                    } else {
                        String::new()
                    }),
                )
            })
            .collect();
        let mut col = Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Default agent workflow", t0.accent)]))
            .child(line(vec![span(
                "[catalog:]bundle[@version]:flow — no version = the latest published; empty = the built-in default",
                t0.text_faint,
            )]));
        for (a, (_, sig)) in current.agent_defaults.iter().zip(fields.iter()) {
            let builtin = if a.builtin.is_empty() {
                "none".to_string()
            } else {
                a.builtin.clone()
            };
            col = col
                .child(field(
                    &t0,
                    &a.interface,
                    TextInput::new()
                        .value(*sig)
                        .placeholder(format!(
                            "built-in: {builtin} · now: {}",
                            if a.available {
                                a.workflow_id.as_str()
                            } else {
                                "unavailable"
                            }
                        ))
                        .layout(LayoutStyle::default().w(64).h(1))
                        .element(mcx, &t0)
                        .build(),
                ))
                .child(line(vec![span(
                    ellipsize(
                        &if a.eligible.is_empty() {
                            "no workflow on this gateway declares this interface".to_string()
                        } else {
                            format!("choices: {}", a.eligible.join(" · "))
                        },
                        106,
                    ),
                    t0.text_faint,
                )]));
            if !a.available {
                col = col.child(line(vec![span(
                    ellipsize(&format!("⚠ {}", a.reason), 106),
                    t0.warn,
                )]));
            }
        }
        let ctx_save = ctx2.clone();
        let close_cancel = close.clone();
        let agents_now = current.agent_defaults.clone();
        col.child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Save")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let typed: Vec<(String, String)> = fields
                                    .iter()
                                    .map(|(n, sig)| (n.clone(), sig.get_untracked()))
                                    .collect();
                                let body = agent_defaults_body(&agents_now, &typed);
                                if body.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                                    form_error.set(Some("nothing changed".into()));
                                    return;
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_save.send(Cmd::SaveRuntimeConfig {
                                    body: body.into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
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

fn open_skills_shelf_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let Some(shelf) = current.skills_shelf.clone() else {
        return;
    };
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(110, 14), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let sig = mcx.signal(if shelf.source == "stored" {
            shelf.value.clone()
        } else {
            String::new()
        });
        let ctx_save = ctx2.clone();
        let close_cancel = close.clone();
        let shelf_now = shelf.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Skills shelf", t0.accent)]))
            .child(line(vec![span(
                "a folder holding skills/<name>/SKILL.md; empty = the gateway's own copy of the curated shelf",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "folder",
                TextInput::new()
                    .value(sig)
                    .placeholder(format!("{} (now: {} · {})", shelf.default_path, if shelf.available { shelf.resolved.as_str() } else { "unavailable" }, shelf.source))
                    .layout(LayoutStyle::default().w(80).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Save")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let body = skills_shelf_body(&shelf_now, &sig.get_untracked());
                                if body.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                                    form_error.set(Some("nothing changed".into()));
                                    return;
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_save.send(Cmd::SaveRuntimeConfig {
                                    body: body.into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

/// Browser-apps settings form (mission Z): one line per `apps.*` setting,
/// prefilled with the STORED value only (a resolved env/default value
/// written back would silently become a stored one); the gateway
/// validates and its sentence is shown on refusal.
pub(crate) fn open_apps_settings_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let ctx2 = ctx.clone();
    open_form(
        ctx,
        cx,
        Size::new(
            96,
            11 + current
                .apps
                .iter()
                .map(|a| 1 + super::util::wrap_text(&a.help, 92).len().max(1) as i32)
                .sum::<i32>(),
        ),
        move |mcx, close| {
            let theme = use_theme(mcx);
            let t0 = theme.get().tokens;
            let form_error = mcx.signal(Option::<String>::None);
            let in_flight = mcx.signal(false);
            let form_id = crate::worker::next_form_id();
            super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
            let fields: Vec<(String, Signal<String>)> = current
                .apps
                .iter()
                .map(|a| {
                    (
                        a.name.clone(),
                        mcx.signal(if a.source == "stored" {
                            a.value.clone()
                        } else {
                            String::new()
                        }),
                    )
                })
                .collect();
            let mut col = Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Browser apps settings", t0.accent)]))
            .children(
                super::util::wrap_text(
                    "Empty = the default (or the value this gateway's environment gives). Applies at the next app start or download.",
                    92,
                )
                .into_iter()
                .map(|l| line(vec![span(l, t0.text_faint)]))
                .collect::<Vec<_>>(),
            );
            for (a, (_, sig)) in current.apps.iter().zip(fields.iter()) {
                col = col.child(field(
                    &t0,
                    &a.label,
                    TextInput::new()
                        .value(*sig)
                        .placeholder(format!(
                            "{} (now: {} · {})",
                            a.placeholder,
                            if a.value.is_empty() {
                                "—"
                            } else {
                                a.value.as_str()
                            },
                            a.source
                        ))
                        .layout(LayoutStyle::default().w(60).h(1))
                        .element(mcx, &t0)
                        .build(),
                ));
                // The whole help sentence, wrapped (never cut).
                for l in super::util::wrap_text(&a.help, 92) {
                    col = col.child(line(vec![span(l, t0.text_faint)]));
                }
            }
            let ctx_save = ctx2.clone();
            let close_cancel = close.clone();
            let apps_now = current.apps.clone();
            col.child(super::message_slot(theme, form_error, in_flight))
                .child(
                    Element::new()
                        .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                        .child(
                            Button::new("Save")
                                .on_click(move || {
                                    if in_flight.get_untracked() {
                                        return;
                                    }
                                    let typed: Vec<(String, String)> = fields
                                        .iter()
                                        .map(|(n, sig)| (n.clone(), sig.get_untracked()))
                                        .collect();
                                    let body = apps_settings_body(&apps_now, &typed);
                                    if body.as_object().map(|m| m.is_empty()).unwrap_or(true) {
                                        form_error.set(Some("nothing changed".into()));
                                        return;
                                    }
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_save.send(Cmd::SaveRuntimeConfig {
                                        body: body.into(),
                                        form_id: Some(form_id),
                                    });
                                })
                                .element(mcx, &t0)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_cancel())
                                .element(mcx, &t0)
                                .build(),
                        )
                        .build(),
                )
                .build()
        },
    );
}

/// The gateway-wide workspace defaults form, as the web models it
/// (console.py renderGatewayPolicyModal / saveWorkspacePolicyModal).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct WorkspaceDefaults {
    pub root: String,
    pub allowed: String,
    pub blocked: String,
    /// "whitelist" | "blacklist".
    pub mode: String,
    /// Launch-folder trust: None = inherit (not stored).
    pub trust: Option<bool>,
    /// The legacy full bypass: None = inherit (not stored).
    pub overrides: Option<bool>,
}

impl WorkspaceDefaults {
    /// The form's prefill: only STORED choices for the root and the two
    /// switches (a resolved env/default value written back would become a
    /// stored setting); the path lists and the posture as served.
    pub fn prefill(c: &RuntimeConfigData) -> WorkspaceDefaults {
        let stored = |source: &str| source == "stored";
        WorkspaceDefaults {
            root: if stored(&c.workspace_root_source) {
                c.workspace_root.clone()
            } else {
                String::new()
            },
            allowed: c.workspace_allowed_paths.clone(),
            blocked: c.workspace_blocked_paths.clone(),
            mode: if c.workspace_default_mode == "blacklist" {
                "blacklist".into()
            } else {
                "whitelist".into()
            },
            trust: stored(&c.trust_client_launch_folder_source)
                .then_some(c.trust_client_launch_folder),
            overrides: stored(&c.client_workspace_scope_overrides_source)
                .then_some(c.client_workspace_scope_overrides),
        }
    }

    /// `POST /admin/runtime-config` body, the web's exact shape: the root
    /// is null when blank (inherit), trust is null when inherited, the
    /// bypass is named only when chosen. `user_workspace_policies` is
    /// DELIBERATELY absent: present-but-empty deletes the whole per-user
    /// map server-side (per-user edits ride the single-entry PUT).
    pub fn body(&self) -> Value {
        let root = self.root.trim();
        let mode = if self.mode.is_empty() {
            "whitelist"
        } else {
            self.mode.as_str()
        };
        let mut body = json!({
            "workspace_default_mode": mode,
            "workspace_root": if root.is_empty() { Value::Null } else { Value::String(root.to_string()) },
            "workspace_allowed_paths": self.allowed.trim(),
            "workspace_blocked_paths": self.blocked.trim(),
            "trust_client_launch_folder": self.trust.map(Value::Bool).unwrap_or(Value::Null),
        });
        if let Some(on) = self.overrides {
            body["client_workspace_scope_overrides"] = Value::Bool(on);
        }
        body
    }
}

/// Tri-state select index: 0 = inherit, 1 = on, 2 = off.
fn tri_index(v: Option<bool>) -> usize {
    match v {
        None => 0,
        Some(true) => 1,
        Some(false) => 2,
    }
}

fn tri_value(ix: usize) -> Option<bool> {
    match ix {
        1 => Some(true),
        2 => Some(false),
        _ => None,
    }
}

fn open_workspace_policy_form(cx: Scope, ctx: &Ctx, current: RuntimeConfigData) {
    if !current.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(92, 32), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        // Only STORED choices prefill (the web's renderGatewayPolicyModal):
        // writing a resolved env/default value back would silently promote
        // it to a stored setting.
        let pre = WorkspaceDefaults::prefill(&current);
        let workspace_root = mcx.signal(pre.root.clone());
        let workspace_allowed_paths = mcx.signal(pre.allowed.clone());
        let workspace_blocked_paths = mcx.signal(pre.blocked.clone());
        let allow_client_scope = mcx.signal(tri_index(pre.overrides));
        let trust_launch_folder = mcx.signal(tri_index(pre.trust));
        let default_mode = mcx.signal(pre.mode.clone());
        let allowed_state = TextAreaState::new(mcx);
        allowed_state.set_text(pre.allowed.clone());
        let blocked_state = TextAreaState::new(mcx);
        blocked_state.set_text(pre.blocked.clone());
        let root_placeholder = if current.workspace_root.trim().is_empty() {
            "blank = inherit (env/default fallback)".to_string()
        } else {
            format!("blank = inherit ({})", current.workspace_root.trim())
        };
        let inherit_label = |now: bool| {
            SelectOption::new(format!("inherit (now {})", if now { "on" } else { "off" }))
        };
        let trust_options = vec![
            inherit_label(current.trust_client_launch_folder),
            SelectOption::new("on"),
            SelectOption::new("off"),
        ];
        let overrides_options = vec![
            inherit_label(current.client_workspace_scope_overrides),
            SelectOption::new("on"),
            SelectOption::new("off"),
        ];
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();

        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let ctx_save = ctx2.clone();
        let close_cancel = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                "Workspace access policy",
                t0.accent,
            )]))
            .child(line(vec![span(
                "choose whether trusted clients may use their launch folder, plus extra allowed and blocked workspaces",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "default workspace",
                TextInput::new()
                    .value(workspace_root)
                    .placeholder(root_placeholder)
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(60).h(1))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "launch folder trust",
                Select::new(trust_options)
                    .value(trust_launch_folder)
                    .layout(LayoutStyle::default().w(24).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(
                "  an agent may write in the folder it was started from (gateway default on)",
                t0.text_faint,
            )]))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let label = if default_mode.get() == "blacklist" {
                    "allow everything, refuse listed folders (wide grant)"
                } else {
                    "deny everything, allow listed folders (shipped default)"
                };
                line(vec![span("default posture: ", t0.text_muted), span(label, t0.text)])
            }))
            .child(
                Button::new("change default posture")
                    .on_click(move || {
                        default_mode.update(|m| {
                            *m = if m == "blacklist" { "whitelist".to_string() } else { "blacklist".to_string() }
                        });
                    })
                    .element(mcx, &t0)
                    .build(),
            )
            .child(field(
                &t0,
                "full bypass (legacy)",
                Select::new(overrides_options)
                    .value(allow_client_scope)
                    .layout(LayoutStyle::default().w(24).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(
                "  unlike launch-folder trust (one folder), clients may scope ANY server folder — postures stop applying",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "allowed workspaces",
                TextArea::new()
                    .state(&allowed_state)
                    .placeholder("/abs/path, one per line")
                    .on_change(move |s: &str| {
                        if workspace_allowed_paths.with_untracked(|cur| cur != s) {
                            workspace_allowed_paths.set(s.to_string());
                        }
                    })
                    .submit_policy(SubmitPolicy::EnterInserts)
                    .rows(4, 6)
                    .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "blocked workspaces",
                TextArea::new()
                    .state(&blocked_state)
                    .placeholder("/abs/path, one per line")
                    .on_change(move |s: &str| {
                        if workspace_blocked_paths.with_untracked(|cur| cur != s) {
                            workspace_blocked_paths.set(s.to_string());
                        }
                    })
                    .submit_policy(SubmitPolicy::EnterInserts)
                    .rows(3, 5)
                    .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(line(vec![span(
                "per-user policies: press w on a user runtime row (single-entry saves — this form never touches them)",
                t0.text_faint,
            )]))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Save")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let body = WorkspaceDefaults {
                                    root: workspace_root.get_untracked(),
                                    allowed: workspace_allowed_paths.get_untracked(),
                                    blocked: workspace_blocked_paths.get_untracked(),
                                    mode: default_mode.get_untracked(),
                                    trust: tri_value(trust_launch_folder.get_untracked()),
                                    overrides: tri_value(allow_client_scope.get_untracked()),
                                }
                                .body();
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_save.send(Cmd::SaveRuntimeConfig {
                                    body: body.into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

/// How many accounts carry a custom workspace policy (parsed from the
/// knobs payload's per-user map — one source for the header summary and
/// the knobs row).
fn user_policy_count(d: &RuntimeConfigData) -> usize {
    serde_json::from_str::<Value>(&d.user_workspace_policies)
        .ok()
        .and_then(|v| v.as_object().map(|m| m.len()))
        .unwrap_or(0)
}

/// ONE user's stored policy entry from the knobs payload ({} = inherits).
fn user_policy_entry(d: &RuntimeConfigData, key: &str) -> serde_json::Map<String, Value> {
    serde_json::from_str::<Value>(&d.user_workspace_policies)
        .ok()
        .and_then(|v| v.get(key).and_then(Value::as_object).cloned())
        .unwrap_or_default()
}

/// The Accounts screen's Workspace action for a user (admin): the same
/// per-user policy form as `w` here, loading the runtime config first
/// when it was never read (one more keypress, said so).
pub fn open_user_policy(cx: Scope, ctx: &Ctx, tenant: String, user: String) {
    let config = match ctx.store.runtime_config.get_untracked() {
        Loadable::Ready(d) => d,
        Loadable::Failed(e) => {
            ctx.store.runtime_config.set(Loadable::Loading);
            ctx.send(Cmd::LoadRuntimeConfig);
            ctx.store.notice.set(Some(format!(
                "couldn't read the workspace config ({e}) — reading again; press w in a moment"
            )));
            return;
        }
        Loadable::NotAsked => {
            ctx.store.runtime_config.set(Loadable::Loading);
            ctx.send(Cmd::LoadRuntimeConfig);
            ctx.store.notice.set(Some(
                "loading workspace config — press w again in a moment".into(),
            ));
            return;
        }
        Loadable::Loading => {
            ctx.store.notice.set(Some(
                "workspace config still loading — try again in a moment".into(),
            ));
            return;
        }
    };
    if !config.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    open_user_policy_form(cx, ctx, &config, tenant, user);
}

/// `w` on the highlighted INVENTORY row (users.rs `e/t/d` idiom): open the
/// per-user workspace policy form. Refuses, with a notice naming why, on
/// rows that resolve to no single principal.
fn open_user_policy_for_selected(cx: Scope, ctx: &Ctx) {
    let idx = ctx.ui.runtime_sel.get_untracked();
    let row = ctx
        .store
        .runtimes
        .with_untracked(|d| d.ready().and_then(|rows| rows.get(idx).cloned()));
    let Some(row) = row else {
        ctx.store
            .notice
            .set(Some("no runtime highlighted — nothing to configure".into()));
        return;
    };
    if row.kind == "entity" {
        ctx.store.notice.set(Some(
            "entity filesystem access is configured on the entity itself (workspace mounts), not here".into(),
        ));
        return;
    }
    if row.owners.len() != 1 {
        ctx.store.notice.set(Some(if row.owners.is_empty() {
            "no live user binds this plane — there is no principal to configure".to_string()
        } else {
            "several users bind this plane — configure each user from screen 2 (Accounts, w)"
                .to_string()
        }));
        return;
    }
    let config = match ctx.store.runtime_config.get_untracked() {
        Loadable::Ready(d) => d,
        Loadable::NotAsked => {
            // Lazy-load law: nothing loads on screen entry, so the first
            // `w` fires the load itself and asks for one more keypress.
            ctx.store.runtime_config.set(Loadable::Loading);
            ctx.send(Cmd::LoadRuntimeConfig);
            ctx.store.notice.set(Some(
                "loading workspace config — press w again in a moment".into(),
            ));
            return;
        }
        _ => {
            ctx.store.notice.set(Some(
                "runtime config still loading — try again in a moment".into(),
            ));
            return;
        }
    };
    if !config.writable {
        ctx.store
            .notice
            .set(Some("this needs an admin token".into()));
        return;
    }
    let tenant = if row.tenant_id.trim().is_empty() {
        "default".to_string()
    } else {
        row.tenant_id.clone()
    };
    let user = row.owners[0].clone();
    open_user_policy_form(cx, ctx, &config, tenant, user);
}

/// The per-user workspace policy form (operator order 2026-08-19: settings
/// live ON the runtime, with an explicit posture choice — deny-all+allow
/// list, or allow-all+refuse list). Three-state fields cycle on click;
/// blank = inherit. Save PUTs the SINGLE entry — never the map.
fn open_user_policy_form(
    cx: Scope,
    ctx: &Ctx,
    config: &RuntimeConfigData,
    tenant: String,
    user: String,
) {
    let key = format!("{tenant}:{user}");
    let entry = user_policy_entry(config, &key);
    let customized = !entry.is_empty();
    let str_of = |v: Option<&Value>| -> String {
        v.and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_default()
    };
    let tri_of = |v: Option<&Value>| -> String {
        match v.and_then(Value::as_bool) {
            Some(true) => "on".to_string(),
            Some(false) => "off".to_string(),
            None => String::new(),
        }
    };
    let lines_of = |v: Option<&Value>| -> String {
        v.and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join("\n")
            })
            .unwrap_or_default()
    };
    let mode0 = str_of(entry.get("mode"));
    let trust0 = tri_of(entry.get("trust_client_launch_folder"));
    let overrides0 = tri_of(entry.get("client_workspace_scope_overrides"));
    let allowed0 = lines_of(entry.get("workspace_allowed_paths"));
    let blocked0 = lines_of(entry.get("workspace_blocked_paths"));

    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(92, 30), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let mode = mcx.signal(mode0.clone());
        let trust = mcx.signal(trust0.clone());
        let overrides = mcx.signal(overrides0.clone());
        let allowed = mcx.signal(allowed0.clone());
        let blocked = mcx.signal(blocked0.clone());
        let allowed_state = TextAreaState::new(mcx);
        allowed_state.set_text(allowed0.clone());
        let blocked_state = TextAreaState::new(mcx);
        blocked_state.set_text(blocked0.clone());
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();

        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let tenant_save = tenant.clone();
        let user_save = user.clone();
        let tenant_reset = tenant.clone();
        let user_reset = user.clone();
        let ctx_save = ctx2.clone();
        let ctx_reset = ctx2.clone();
        let close_cancel = close.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Workspace policy — {key}"),
                t0.accent,
            )]))
            .child(line(vec![span(
                if customized {
                    "this user has a custom policy · blank/inherit fields fall back to the gateway defaults"
                } else {
                    "this user inherits the gateway defaults · anything you set here overrides them"
                },
                t0.text_faint,
            )]))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let label = match trust.get().as_str() {
                    "on" => "on — agents may write in the folder they are started from",
                    "off" => "off — launch folders get no special treatment",
                    _ => "inherit gateway default",
                };
                line(vec![span("launch-folder trust: ", t0.text_muted), span(label, t0.text)])
            }))
            .child(
                Button::new("change launch-folder trust")
                    .on_click(move || {
                        trust.update(|v| {
                            *v = match v.as_str() {
                                "" => "on".to_string(),
                                "on" => "off".to_string(),
                                _ => String::new(),
                            }
                        });
                    })
                    .element(mcx, &t0)
                    .build(),
            )
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let label = match mode.get().as_str() {
                    "whitelist" => "deny everything, allow listed folders",
                    "blacklist" => "allow everything, refuse listed folders",
                    _ => "inherit — deny everything, allow listed folders (gateway default)",
                };
                line(vec![span("posture: ", t0.text_muted), span(label, t0.text)])
            }))
            .child(
                Button::new("change posture")
                    .on_click(move || {
                        mode.update(|m| {
                            *m = match m.as_str() {
                                "" => "whitelist".to_string(),
                                "whitelist" => "blacklist".to_string(),
                                _ => String::new(),
                            }
                        });
                    })
                    .element(mcx, &t0)
                    .build(),
            )
            .child(field(
                &t0,
                "allowed folders",
                TextArea::new()
                    .state(&allowed_state)
                    .placeholder("/abs/path, one per line — extra roots on top of the gateway-wide ones")
                    .on_change(move |s: &str| {
                        if allowed.with_untracked(|cur| cur != s) {
                            allowed.set(s.to_string());
                        }
                    })
                    .submit_policy(SubmitPolicy::EnterInserts)
                    .rows(3, 5)
                    .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(field(
                &t0,
                "refused folders",
                TextArea::new()
                    .state(&blocked_state)
                    .placeholder("/abs/path, one per line — denied in every posture")
                    .on_change(move |s: &str| {
                        if blocked.with_untracked(|cur| cur != s) {
                            blocked.set(s.to_string());
                        }
                    })
                    .submit_policy(SubmitPolicy::EnterInserts)
                    .rows(3, 5)
                    .layout(LayoutStyle::default().basis(Dimension::Cells(0)).grow(1.0))
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                let label = match overrides.get().as_str() {
                    "on" => "granted — clients may scope ANY server path",
                    "off" => "refused",
                    _ => "inherit gateway default",
                };
                line(vec![span("scope overrides (admin-classed): ", t0.text_muted), span(label, t0.text)])
            }))
            .child(
                Button::new("change scope overrides")
                    .on_click(move || {
                        overrides.update(|v| {
                            *v = match v.as_str() {
                                "" => "on".to_string(),
                                "on" => "off".to_string(),
                                _ => String::new(),
                            }
                        });
                    })
                    .element(mcx, &t0)
                    .build(),
            )
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Save")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                let mut policy = serde_json::Map::new();
                                let m = mode.get_untracked();
                                if !m.is_empty() {
                                    policy.insert("mode".into(), Value::String(m));
                                }
                                let tr = trust.get_untracked();
                                if !tr.is_empty() {
                                    policy.insert(
                                        "trust_client_launch_folder".into(),
                                        Value::Bool(tr == "on"),
                                    );
                                }
                                let ov = overrides.get_untracked();
                                if !ov.is_empty() {
                                    policy.insert(
                                        "client_workspace_scope_overrides".into(),
                                        Value::Bool(ov == "on"),
                                    );
                                }
                                let list = |raw: &str| -> Vec<Value> {
                                    raw.lines()
                                        .map(str::trim)
                                        .filter(|l| !l.is_empty())
                                        .map(|l| Value::String(l.to_string()))
                                        .collect()
                                };
                                let al = list(&allowed.get_untracked());
                                if !al.is_empty() {
                                    policy.insert("workspace_allowed_paths".into(), Value::Array(al));
                                }
                                let bl = list(&blocked.get_untracked());
                                if !bl.is_empty() {
                                    policy.insert("workspace_blocked_paths".into(), Value::Array(bl));
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_save.send(Cmd::SaveUserWorkspacePolicy {
                                    tenant_id: tenant_save.clone(),
                                    user_id: user_save.clone(),
                                    body: json!({ "policy": Value::Object(policy) }).into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Reset to inherited")
                            .on_click(move || {
                                if in_flight.get_untracked() {
                                    return;
                                }
                                form_error.set(None);
                                in_flight.set(true);
                                ctx_reset.send(Cmd::SaveUserWorkspacePolicy {
                                    tenant_id: tenant_reset.clone(),
                                    user_id: user_reset.clone(),
                                    body: json!({ "policy": Value::Null }).into(),
                                    form_id: Some(form_id),
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

/// The Sessions tab: the chosen plane's top-level runs (spinner while
/// the per-plane load runs; entity planes explain why empty is normal).
/// `cx` is the PAGE scope — the steer modal opens on it so a runs
/// reload landing mid-form cannot dispose the form's signals.
/// The inventory table's height: header + rows, at least 2 and at most
/// 40% of the terminal's height.
pub fn inventory_rows(n: usize, term_h: i32) -> i32 {
    let want = n as i32 + 1;
    want.clamp(2, (term_h * 2 / 5).max(2))
}

/// The Runs tab's page-scoped state (survives the panel's re-renders).
#[derive(Clone, Copy)]
struct RunsPanelState {
    root_only: Signal<bool>,
    expanded: Signal<Option<usize>>,
    confirm: super::kit::InlineConfirm,
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

fn sessions_panel(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    row: &RuntimeRow,
    st: RunsPanelState,
) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let ctx_bar = ctx.clone();
    let tt = *t;
    let scope = RunScope::of_runtime(row);
    let scope_hint = scope.actionable();
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(dyn_view_scoped(
            LayoutStyle::line(1).shrink(0.0),
            move |bcx| {
                let _ = bcx;
                if !toolbar_fits(cx) {
                    return line(vec![]);
                }
                let opts: Vec<(String, String)> = [
                    ("", "all statuses"),
                    ("running", "running"),
                    ("waiting", "waiting"),
                    ("completed", "completed"),
                    ("failed", "failed"),
                    ("cancelled", "cancelled"),
                ]
                .iter()
                .map(|(v, l)| ((*v).to_string(), (*l).to_string()))
                .collect();
                toolbar_row(
                    cx,
                    &ctx_bar,
                    &tt,
                    opts,
                    ui.rt_runs_status,
                    ui.rt_runs_query,
                    "search runs — run id, workflow, session · *glob* (Enter)",
                    move || ui.rt_runs_offset.set(0),
                )
            },
        ))
        .child(dyn_view(
            LayoutStyle::column().gap(0).shrink(0.0),
            move || {
                // State line (also the WHOLE toolbar on short terminals — the
                // documented 0240 starvation class): filter + query + page
                // position + the gestures that change them.
                // Hidden while the Cancel confirm holds the panel.
                if st.confirm.pending.with(Option::is_some) {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                let text = match store.runs.get() {
                    Loadable::Loading => "loading this runtime's runs…".to_string(),
                    Loadable::Ready(d) => {
                        let mut bits: Vec<String> = Vec::new();
                        bits.push(format!("showing: {}", d.scope.describe()));
                        if d.scope.actionable() {
                            bits.push(format!(
                                "{} (t)",
                                super::switch::switch_text(
                                    "Root runs only",
                                    st.root_only.get(),
                                    None,
                                    false
                                )
                            ));
                        } else if !d.root_only {
                            bits.push("incl. children (plane view)".to_string());
                        }
                        if !d.status.is_empty() {
                            bits.push(format!("status={}", d.status));
                        }
                        if !d.query.is_empty() {
                            bits.push(query_bit(&d.query));
                        }
                        bits.push(page_label(d.offset, d.rows.len(), d.has_more, None));
                        if !scope_hint {
                            bits.push("read-only plane".to_string());
                        }
                        bits.join(" · ")
                    }
                    _ => String::new(),
                };
                // Wrapped: the switch and the page position never fall off the edge.
                let w = (abstracttui::app::use_viewport(cx).get_untracked().w
                    - widths::BLOCK_CHROME
                    - 2)
                .max(20) as usize;
                Element::new()
                    .style(LayoutStyle::column().gap(0).shrink(0.0))
                    .children(
                        super::util::wrap_text(&text, w)
                            .into_iter()
                            .map(|l| line(vec![span(l, tt.text_faint)]))
                            .collect::<Vec<_>>(),
                    )
                    .build()
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0).min_h(1),
            move |gcx| {
                let data = store.runs.get();
                // The web's empty sentences (filters named; the read-only
                // plane's own sentence).
                let empty = match &data {
                    Loadable::Ready(d) => runs_empty_text(&d.scope, &d.status, &d.query),
                    _ => "No runs yet.".to_string(),
                };
                let w = abstracttui::app::use_viewport(gcx).get().w - widths::BLOCK_CHROME - 2;
                let body: View = loadable_view(
                    &tt,
                    &store.conn.get(),
                    || store.tick.get(),
                    &data,
                    |d: &RunsData| d.rows.is_empty(),
                    &empty,
                    |d| runs_table(gcx, &tt, &d.rows, ui.run_sel, st.expanded),
                );
                // While the Cancel confirm is open it takes the table's
                // place (it names the run): the panel never overflows.
                if st.confirm.pending.with(Option::is_some) {
                    return st.confirm.view(&tt, w);
                }
                body
            },
        ))
        .build()
}

/// The Data & cache tab: the chosen plane's on-disk facts plus the
/// registered data-home stores attributed to it (longest data_dir
/// prefix — see `home_plane_index`). The default plane additionally
/// lists SHARED stores (outside every plane: ~/.abstractcore caches),
/// since the default plane is the gateway process's own home. Purge
/// stays here (dry-run gated, worker-enforced).
/// `cx` is the PAGE scope (the purge confirm opens on it — same
/// survival rule as the steer modal); the home_sel clamp is installed
/// once in `view`, never here (per-tab-entry effects accumulate).
/// The Artifacts tab: deliverables metadata — images, video, audio, text
/// runs produced (operator 2026-08-19: never conflated with caches). One
/// gateway-wide index, most recent first; a terminal LISTS — opening the
/// bytes is the web console's Artifacts tab.
fn artifacts_panel(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let ctx_open = ctx.clone();
    let ctx_bar = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(dyn_view_scoped(LayoutStyle::line(1).shrink(0.0), move |bcx| {
            let _ = bcx;
            if !toolbar_fits(cx) {
                return line(vec![]);
            }
            let opts: Vec<(String, String)> = ARTIFACT_TYPES
                .iter()
                .map(|(v, l)| ((*v).to_string(), (*l).to_string()))
                .collect();
            toolbar_row(
                cx,
                &ctx_bar,
                &tt,
                opts,
                ui.rt_art_modality,
                ui.rt_art_query,
                "search artifacts — name, kind, tags, 2026-06-13, *.jpg (Enter)",
                move || ui.rt_art_offset.set(0),
            )
        }))
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0).min_h(1),
            move |gcx| {
                let data = store.artifacts.get();
                let ctx_row = ctx_open.clone();
                loadable_view(
                    &tt,
                    &store.conn.get(),
                    || store.tick.get(),
                    &data,
                    |d: &crate::store::ArtifactsData| d.rows.is_empty(),
                    "no artifacts match — runs that produce files, images, audio, or video list them here",
                    |d| {
                        let rows_for_open = d.rows.clone();
                        // The web Artifacts table's columns, as wrapping
                        // rows (R7.2: an id or a workflow is never cut).
                        let body: Vec<super::kit::Row> = d
                            .rows
                            .iter()
                            .map(|a| {
                                super::kit::Row::new(vec![
                                    a.name.clone(),
                                    a.kind.clone(),
                                    a.size_bytes.map(human_bytes).unwrap_or_else(|| "—".into()),
                                    if a.workflow_id.is_empty() { "—".into() } else { a.workflow_id.clone() },
                                    if a.run_id.is_empty() { "—".into() } else { a.run_id.clone() },
                                    a.created_at.replace('T', " ").chars().take(19).collect(),
                                ])
                            })
                            .collect();
                        let rules = vec![
                            widths::ColRule::tail("Artifact", 12),
                            widths::ColRule::head("Type", 4),
                            widths::ColRule::head("Size", 8),
                            widths::ColRule::tail("Workflow", 10),
                            widths::ColRule::tail("Run", 8),
                            widths::ColRule::head("Created", 19),
                        ];
                        // Enter opens the artifact (never on mere highlight:
                        // a preview per arrow key would fetch bytes).
                        super::kit::WrapTable::new(rules, body, ui.rt_art_sel)
                            .on_activate(move |idx| {
                                if let Some(a) = rows_for_open.get(idx) {
                                    open_artifact_detail(cx, &ctx_row, a.clone());
                                }
                            })
                            .layout(LayoutStyle::default().grow(1.0).min_h(2))
                            .element(gcx, &tt)
                            .build()
                    },
                )
            },
        ))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let text = match store.artifacts.get() {
                Loadable::Loading => "loading artifacts…".to_string(),
                Loadable::Ready(d) => {
                    let mut bits: Vec<String> = Vec::new();
                    if !d.modality.is_empty() {
                        bits.push(format!("type={}", modality_label(&d.modality)));
                    }
                    if !d.query.is_empty() {
                        bits.push(query_bit(&d.query));
                    }
                    bits.push(page_label(d.offset, d.rows.len(), d.has_more, Some(d.total)));
                    format!("{} · Enter opens", bits.join(" · "))
                }
                _ => String::new(),
            };
            line(vec![span(text, tt.text_faint)])
        }))
        .build()
}

/// The `q="…"` bit of a tab's note line. A glob SAYS so: `*.jpg` and
/// `photo.jpg` are read by different halves of the query language, and a
/// zero-row answer is only diagnosable if the note names the half.
fn query_bit(query: &str) -> String {
    if Needle::new(query).is_glob() {
        format!("q=\"{query}\" (glob)")
    } else {
        format!("q=\"{query}\"")
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

/// One artifact's detail: metadata always, a TEXT preview when the kind
/// and size allow it. A terminal cannot render image/video/audio — that
/// is said plainly instead of pretending (web-console parity means the
/// same FACTS, not the same pixels).
fn open_artifact_detail(cx: Scope, ctx: &Ctx, a: crate::store::ArtifactRow) {
    let ctx2 = ctx.clone();
    // A previous preview must never paint under this artifact's header.
    // Clearing the slots is not enough on its own: the worker is one
    // serial lane, so a result for the artifact opened a moment ago
    // arrives AFTER this open and lands in the same slot. Stamping the
    // target is what lets the worker drop it.
    ctx.store.artifact_text.set(None);
    ctx.store.artifact_image.set(None);
    ctx.store
        .preview_target
        .set(crate::store::artifact_preview_key(
            &a.run_id,
            &a.artifact_id,
        ));
    let store_img = ctx.store;
    // Two thirds of the terminal, not a fixed 96x26 — see `preview_size`.
    // The image mosaic and the text window both scale with the space;
    // the metadata header does not.
    let size = super::preview_size(cx);
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let preview = mcx.signal(String::new());
        let art_top = mcx.signal(0i32);
        let close_btn = close.clone();
        // Text-ish kinds fetch a capped window; everything else states why not.
        {
            let a2 = a.clone();
            let ctx_fetch = ctx2.clone();
            mcx.effect(move || {
                if !preview.get_untracked().is_empty() {
                    return;
                }
                let textish = matches!(
                    a2.kind.as_str(),
                    "text" | "markdown" | "json" | "code" | "html"
                );
                let msg = if a2.run_id.is_empty() || a2.artifact_id.is_empty() {
                    "no run-scoped content route for this artifact (no run id) — metadata only".to_string()
                } else if a2.kind == "image" {
                    // abstracttui decodes PNG/JPEG and renders bitmaps as
                    // cell mosaics (pixel protocols where the terminal
                    // supports them) — so images preview HERE.
                    preview.set("decoding image…".to_string());
                    ctx_fetch.send(Cmd::LoadArtifactImage {
                        run_id: a2.run_id.clone(),
                        artifact_id: a2.artifact_id.clone(),
                    });
                    return;
                } else if !textish {
                    format!(
                        "{} artifacts do not render in a terminal — open this one in the web console's Artifacts tab",
                        a2.kind
                    )
                } else if a2.size_bytes.unwrap_or(0) > 256 * 1024 {
                    format!(
                        "too large for a terminal preview ({}) — open it in the web console",
                        a2.size_bytes.map(human_bytes).unwrap_or_default()
                    )
                } else {
                    preview.set("loading preview…".to_string());
                    ctx_fetch.send(Cmd::LoadArtifactText {
                        run_id: a2.run_id.clone(),
                        artifact_id: a2.artifact_id.clone(),
                    });
                    return;
                };
                preview.set(msg);
            });
        }
        // The worker publishes the fetched text into the store slot.
        {
            let store = ctx2.store;
            mcx.effect(move || {
                if let Some(text) = store.artifact_text.get() {
                    preview.set(text);
                }
            });
        }
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(a.name.clone(), t0.accent)]))
            .child(line(vec![span(
                format!(
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
                ),
                t0.text_faint,
            )]))
            .child(line(vec![span(
                format!(
                    "workflow: {} · run: {} · session: {}",
                    if a.workflow_id.is_empty() {
                        "—"
                    } else {
                        &a.workflow_id
                    },
                    if a.run_id.is_empty() {
                        "—"
                    } else {
                        &a.run_id
                    },
                    if a.session_id.is_empty() {
                        "—"
                    } else {
                        &a.session_id
                    },
                ),
                t0.text_muted,
            )]))
            .child(line(vec![
                span("path: ", t0.text_muted),
                span(
                    if a.content_path.is_empty() {
                        "— (served to admins only)".to_string()
                    } else {
                        a.content_path.clone()
                    },
                    t0.text,
                ),
            ]))
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0).min_h(3),
                move |pcx| {
                    if let Some(bmp) = store_img.artifact_image.get() {
                        return Image::from_bitmap(bmp)
                            .fit(abstracttui::widgets::ImageFit::Contain)
                            .layout(LayoutStyle::default().grow(1.0))
                            .view(pcx);
                    }
                    scroll_text_view(pcx, &t0, preview.get(), art_top, None)
                },
            ))
            .child(
                Button::new("Close (Esc)")
                    .on_click(move || close_btn())
                    .element(mcx, &t0)
                    .build(),
            )
            .build()
    });
}

/// The Logs tab: the FILES of this gateway's registered log homes
/// (web-console parity — stale/missing homes are hygiene and live on the
/// Cache tab, never here). Enter tails one file in a modal.
fn logs_panel(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let ctx_open = ctx.clone();
    let ctx_bar = ctx.clone();
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(dyn_view_scoped(
            LayoutStyle::line(1).shrink(0.0),
            move |bcx| {
                let _ = bcx;
                if !toolbar_fits(cx) {
                    return line(vec![]);
                }
                let mut homes: Vec<String> = store
                    .logs
                    .get()
                    .ready()
                    .map(|rows| rows.iter().map(|f| f.home.clone()).collect())
                    .unwrap_or_default();
                homes.sort();
                homes.dedup();
                let mut opts: Vec<(String, String)> =
                    vec![(String::new(), "all log homes".to_string())];
                opts.extend(homes.into_iter().map(|h| (h.clone(), h)));
                toolbar_row(
                    cx,
                    &ctx_bar,
                    &tt,
                    opts,
                    ui.rt_logs_home,
                    ui.rt_logs_query,
                    "search log files — file name, e.g. *.log (Enter)",
                    move || ui.rt_logs_sel.set(0),
                )
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0).min_h(1),
            move |gcx| {
                let data = store.logs.get();
                let home_f = ui.rt_logs_home.get();
                let query_f = ui.rt_logs_query.get();
                let ctx_row = ctx_open.clone();
                loadable_view(
                    &tt,
                    &store.conn.get(),
                    || store.tick.get(),
                    &data,
                    |rows: &Vec<crate::store::LogFileRow>| {
                        filter_log_files(rows, &home_f, &query_f).is_empty()
                    },
                    "no log files match — serving logs live on the gateway default plane",
                    |rows| {
                        let shown = filter_log_files(rows, &home_f, &query_f);
                        let for_open = shown.clone();
                        // The web Logs table's columns, wrapping.
                        let body: Vec<super::kit::Row> = shown
                            .iter()
                            .map(|f| {
                                super::kit::Row::new(vec![
                                    f.name.clone(),
                                    f.home.clone(),
                                    f.size_bytes.map(human_bytes).unwrap_or_else(|| "—".into()),
                                    f.modified_at.replace('T', " ").chars().take(19).collect(),
                                ])
                            })
                            .collect();
                        let rules = vec![
                            widths::ColRule::tail("File", 12),
                            widths::ColRule::tail("Log home", 10),
                            widths::ColRule::head("Size", 8),
                            widths::ColRule::head("Modified", 19),
                        ];
                        super::kit::WrapTable::new(rules, body, ui.rt_logs_sel)
                            .on_activate(move |idx| {
                                if let Some(f) = for_open.get(idx) {
                                    open_log_tail(cx, &ctx_row, f.home.clone(), f.name.clone());
                                }
                            })
                            .layout(LayoutStyle::default().grow(1.0).min_h(2))
                            .element(gcx, &tt)
                            .build()
                    },
                )
            },
        ))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let text = match store.logs.get() {
                Loadable::Loading => "listing log files…".to_string(),
                Loadable::Ready(rows) => {
                    let home_f = ui.rt_logs_home.get();
                    let query_f = ui.rt_logs_query.get();
                    let shown = filter_log_files(&rows, &home_f, &query_f);
                    let mut bits: Vec<String> = Vec::new();
                    if !home_f.is_empty() {
                        bits.push(format!("home={home_f}"));
                    }
                    if !query_f.is_empty() {
                        bits.push(query_bit(&query_f));
                    }
                    bits.push(format!("{} of {} files", shown.len(), rows.len()));
                    format!("{} · Enter tails", bits.join(" · "))
                }
                _ => String::new(),
            };
            line(vec![span(text, tt.text_faint)])
        }))
        .build()
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

/// Tail ONE log file in a modal. Terminal windows are deliberately small
/// (64/256 KB): the payload is JSON-parsed, cloned into the store, then
/// split into lines by the viewer — the web console reads the big ones.
fn open_log_tail(cx: Scope, ctx: &Ctx, home: String, file: String) {
    let ctx2 = ctx.clone();
    ctx.store.log_text.set(None);
    ctx.store
        .preview_target
        .set(crate::store::log_preview_key(&home, &file));
    ctx.send(Cmd::LoadLogText {
        home: home.clone(),
        file: file.clone(),
        max_bytes: 64 * 1024,
    });
    // Log lines are long and logs are tall: this one earns its share of
    // the terminal more than any other panel in the app.
    let size = super::preview_size(cx);
    open_form(ctx, cx, size, move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let store = ctx2.store;
        let log_top = mcx.signal(0i32);
        let big = mcx.signal(false);
        let close_btn = close.clone();
        let ctx_more = ctx2.clone();
        let home_more = home.clone();
        let file_more = file.clone();
        Element::new()
            .focusable()
            .autofocus()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![
                span_bold(file.clone(), t0.accent),
                span(format!("  from {home}"), t0.text_faint),
            ]))
            .child(dyn_view_scoped(
                LayoutStyle::default().grow(1.0).min_h(3),
                move |pcx| {
                    let text = store
                        .log_text
                        .get()
                        .unwrap_or_else(|| "reading tail…".to_string());
                    scroll_text_view(pcx, &t0, text, log_top, None)
                },
            ))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Read 256 KB")
                            .on_click(move || {
                                if big.get_untracked() {
                                    return;
                                }
                                big.set(true);
                                ctx_more.store.log_text.set(None);
                                ctx_more.send(Cmd::LoadLogText {
                                    home: home_more.clone(),
                                    file: file_more.clone(),
                                    max_bytes: 256 * 1024,
                                });
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Close (Esc)")
                            .on_click(move || close_btn())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

fn data_panel(cx: Scope, ctx: &Ctx, t: &TokenSet, row: &RuntimeRow) -> View {
    let ctx_bar = ctx.clone();
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let row_facts = row.clone();
    let row_tbl = row.clone();
    let row_purge = row.clone();
    let ctx_purge = ctx.clone();

    let size = match row.size_bytes {
        Some(n) if row.size_note.is_some() => format!("≥ {}", human_bytes(n)),
        Some(n) => human_bytes(n),
        None => "—".into(),
    };
    // An unmaterialized plane (a binding never used) serves
    // data_dir: null — an honest dash, never a blank after the label.
    let dir_text = if row_facts.data_dir.is_empty() {
        "— (no data dir yet: this plane has never materialized)".to_string()
    } else {
        ellipsize(&row_facts.data_dir, 64)
    };
    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![
            span("data dir: ", tt.text_muted),
            span(dir_text, tt.text),
            span(format!("  ·  size on disk: {size}"), tt.text_muted),
        ]))
        .child(dyn_view_scoped(
            LayoutStyle::line(1).shrink(0.0),
            move |bcx| {
                let _ = bcx;
                if !toolbar_fits(cx) {
                    return line(vec![]);
                }
                let mut kinds: Vec<String> = store
                    .data_homes
                    .get()
                    .ready()
                    .map(|rows| {
                        rows.iter()
                            .filter(|h| h.safe_to_purge && h.kind != "logs")
                            .map(|h| h.kind.clone())
                            .collect()
                    })
                    .unwrap_or_default();
                kinds.sort();
                kinds.dedup();
                let mut opts: Vec<(String, String)> =
                    vec![(String::new(), "all kinds".to_string())];
                opts.extend(kinds.into_iter().map(|k| (k.clone(), k)));
                toolbar_row(
                    cx,
                    &ctx_bar,
                    &tt,
                    opts,
                    ui.rt_cache_kind,
                    ui.rt_cache_query,
                    "search caches — name, kind, path · *glob* (Enter)",
                    move || ui.home_sel.set(0),
                )
            },
        ))
        .child(dyn_view_scoped(
            LayoutStyle::default().grow(1.0).min_h(3),
            move |gcx| {
                let data = store.data_homes.get();
                let row_t = row_tbl.clone();
                let kind_f = ui.rt_cache_kind.get();
                let query_f = ui.rt_cache_query.get();
                loadable_view(
                    &tt,
                    &store.conn.get(),
                    || store.tick.get(),
                    &data,
                    |homes: &Vec<DataHomeRow>| {
                        store
                            .runtimes
                            .with(|rt| {
                                filter_homes(
                                    displayed_homes(rt.ready().map_or(&[], |v| v), homes, &row_t),
                                    &kind_f,
                                    &query_f,
                                )
                            })
                            .is_empty()
                    },
                    "no caches match on this plane (the plane's data dir is listed above)",
                    |homes| {
                        let rows = store.runtimes.with(|rt| {
                            filter_homes(
                                displayed_homes(rt.ready().map_or(&[], |v| v), homes, &row_t),
                                &kind_f,
                                &query_f,
                            )
                        });
                        homes_table(gcx, &tt, &rows, ui.home_sel)
                    },
                )
            },
        ))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let idx = ui.home_sel.get();
            let desc = store.data_homes.with(|d| {
                d.ready()
                    .map(|homes| {
                        store.runtimes.with(|rt| {
                            filter_homes(
                                displayed_homes(rt.ready().map_or(&[], |v| v), homes, &row_purge),
                                &ui.rt_cache_kind.get(),
                                &ui.rt_cache_query.get(),
                            )
                        })
                    })
                    .and_then(|rows| rows.get(idx).map(|(h, _)| h.description.clone()))
                    .unwrap_or_default()
            });
            line(vec![span(ellipsize(&desc, 90), tt.text_faint)])
        }))
        .child(dyn_view_scoped(
            LayoutStyle::default().h(1).shrink(0.0),
            move |bcx| {
                let ctx_p = ctx_purge.clone();
                let row_p = ctx_p.ui.rt_detail.get_untracked();
                Element::new()
                    .style(LayoutStyle::row().gap(2))
                    .child(
                        Button::new("Purge selected…")
                            .on_click(move || {
                                let Some(row) = row_p.clone() else { return };
                                // Page scope (`cx`), not this button
                                // region's — the confirm prompt must
                                // survive any inspector re-render.
                                purge_selected(cx, &ctx_p, &row);
                            })
                            .element(bcx, &tt)
                            .build(),
                    )
                    .build()
            },
        ))
        .build()
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

fn homes_table(cx: Scope, t: &TokenSet, rows: &[(DataHomeRow, bool)], sel: Signal<usize>) -> View {
    // The web Cache table's columns (Cache, Kind, Size, Path, Actions) +
    // where the store lives, as wrapping rows: the FULL path, never cut
    // (two data homes differ in their last segment).
    let body: Vec<super::kit::Row> = rows
        .iter()
        .map(|(h, shared)| {
            super::kit::Row::new(vec![
                h.name.clone(),
                h.kind.clone(),
                if !h.exists {
                    "missing".into()
                } else {
                    // "…" while the sized pass is still walking (two-phase).
                    h.size_bytes.map(human_bytes).unwrap_or_else(|| "…".into())
                },
                if *shared {
                    "shared (outside planes)".into()
                } else {
                    "this plane".into()
                },
                if !h.exists {
                    "stale row".into()
                } else {
                    "purgeable".into()
                },
                h.path.clone(),
            ])
        })
        .collect();
    let rules = vec![
        widths::ColRule::tail("Cache", 10),
        widths::ColRule::head("Kind", 6),
        widths::ColRule::head("Size", 8),
        widths::ColRule::head("Where", 10),
        widths::ColRule::head("Actions", 9),
        widths::ColRule::tail("Path", 16),
    ];
    super::kit::WrapTable::new(rules, body, sel)
        .layout(LayoutStyle::default().grow(1.0).min_h(2))
        .element(cx, t)
        .build()
}

/// Purge the Data tab's selected store: same refusal ladder the old
/// modal had (nothing selected / protected), then the dry-run-gated
/// danger confirm.
fn purge_selected(cx: Scope, ctx: &Ctx, row: &RuntimeRow) {
    let idx = ctx.ui.home_sel.get_untracked();
    let target = ctx.store.data_homes.with_untracked(|d| {
        d.ready().and_then(|homes| {
            ctx.store
                .runtimes
                .with_untracked(|rt| {
                    filter_homes(
                        displayed_homes(rt.ready().map_or(&[], |v| v), homes, row),
                        &ctx.ui.rt_cache_kind.get_untracked(),
                        &ctx.ui.rt_cache_query.get_untracked(),
                    )
                })
                .get(idx)
                .map(|(h, _)| h.clone())
        })
    });
    let Some(home) = target else {
        ctx.store.notice.set(Some("no data store selected".into()));
        return;
    };
    if !home.safe_to_purge {
        ctx.store.notice.set(Some(format!(
            "'{}' is protected — the gateway refuses to purge it",
            home.name
        )));
        return;
    }
    confirm_purge_home(cx, ctx, home);
}

fn table(
    cx: Scope,
    t: &TokenSet,
    data: &[RuntimeRow],
    sel: Signal<usize>,
    policy_keys: Option<std::collections::HashSet<String>>,
    keeper: &super::util::FocusKeeper,
    on_choose: impl FnMut(usize) + Clone + 'static,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 108;
    let mut rows: Vec<Vec<String>> = data
        .iter()
        .map(|r| {
            let size = match r.size_bytes {
                // The gateway labels capped walks (#TRUNCATION … floor):
                // an approximate size renders as a floor, never a fact.
                Some(n) if r.size_note.is_some() => format!("≥ {}", human_bytes(n)),
                Some(n) => human_bytes(n),
                None => "—".into(),
            };
            let mut row = vec![
                r.kind.clone(),
                format!("{}/{}", r.tenant_id, r.runtime_id),
                // Uncapped: `ui::widths` sizes this to the row budget.
                r.label.clone(),
            ];
            if wide {
                row.push(if r.owners.is_empty() {
                    "—".into()
                } else {
                    r.owners.join(", ")
                });
            }
            row.push(match (&r.state, &r.liveness) {
                (Some(s), Some(l)) => format!("{s} ({l})"),
                (Some(s), None) => s.clone(),
                _ => "—".into(),
            });
            row.push(size);
            // The policy column TEACHES the gesture in-row (operator
            // 2026-08-19: "how do you want an external user to know it
            // has to press w"): every eligible row names the key; once
            // the config is loaded it also says custom vs inherited.
            row.push(
                if (r.kind == "user" || r.kind == "default") && r.owners.len() == 1 {
                    let tenant = if r.tenant_id.trim().is_empty() {
                        "default"
                    } else {
                        r.tenant_id.as_str()
                    };
                    match &policy_keys {
                        Some(keys) if keys.contains(&format!("{tenant}:{}", r.owners[0])) => {
                            "custom (w edits)".to_string()
                        }
                        Some(_) => "inherited (w edits)".to_string(),
                        None => "press w to edit".to_string(),
                    }
                } else if r.kind == "entity" {
                    "via entity".to_string()
                } else {
                    "—".to_string()
                },
            );
            if wide {
                row.push(r.note.clone().unwrap_or_else(|| "—".into()));
            }
            row
        })
        .collect();
    // `tenant/runtime` and owner lists discriminate on their TAIL; the
    // label and the note are prose and read from the left.
    let mut rules = vec![
        widths::ColRule::head("kind", 8),
        widths::ColRule::tail("runtime", 16),
    ];
    rules.push(widths::ColRule::head("label", 14));
    if wide {
        rules.push(widths::ColRule::tail("owners", 10));
    }
    rules.push(widths::ColRule::head("entity state", 12));
    rules.push(widths::ColRule::head("size", 9));
    rules.push(widths::ColRule::head("workspace policy", 15));
    if wide {
        rules.push(widths::ColRule::head("note", 16));
    }
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    let el = Table::new(cols)
        .rows(rows)
        .selection(sel)
        // Choosing = clicking a row (selection change) or Enter /
        // Space / double-click on the highlighted one. BOTH gestures
        // choose — a keyboard-only operator and a mouse operator get
        // the same contract; nothing loads before one of them fires.
        .on_select(on_choose.clone())
        .on_activate(on_choose)
        // Sized to its rows (header + one per runtime), capped at 40% of
        // the terminal: a two-runtime gateway no longer hands the
        // inventory half the screen of blank rows while the inspector's
        // runs are clipped below it (R7.2, 120x40 capture).
        .layout(LayoutStyle::default().h(inventory_rows(
            data.len(),
            abstracttui::app::use_viewport(cx).get_untracked().h,
        )));
    // The keeper, not a bare `.autofocus()`: a regeneration hands the
    // keyboard back only to a table that held it (design adversary
    // BLOCKER-1: never drag focus back from where the user moved it).
    keeper.wire(el.element(cx, t))
}

/// The selected run + the scope its rows were loaded under (actions
/// must gate on what is DISPLAYED, not on the runtime selection, which
/// may already have moved while the load is in flight).
fn selected_run(ctx: &Ctx) -> Option<(RunRow, RunScope)> {
    let idx = ctx.ui.run_sel.get_untracked();
    ctx.store.runs.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.rows.get(idx).cloned().map(|r| (r, d.scope.clone())))
    })
}

/// ONE cancel entry (key `c`). Refusals name their reason (F2), and a
/// foreign plane refuses BEFORE the confirm: this console's durable
/// commands land in its own principal's inbox — a cancel aimed at
/// another plane's run would be accepted server-side and then sit
/// unconsumed forever (the dishonest "accepted" shape).
fn cancel_selected(cx: Scope, ctx: &Ctx, confirm: super::kit::InlineConfirm) {
    let Some((r, scope)) = selected_run(ctx) else {
        ctx.store
            .notice
            .set(Some("no run selected — nothing to cancel".into()));
        return;
    };
    if !scope.actionable() {
        ctx.store.notice.set(Some(format!(
            "runs in {} are ticked by that plane's own runtime — cancel/steer from this console cannot reach them",
            scope.short()
        )));
        return;
    }
    confirm_cancel(cx, ctx, r, confirm);
}

/// ONE steer entry — shared verbatim by the `s` key and the runs
/// table's activation (Enter / Space / double-click), so the two paths
/// can never drift.
fn steer_selected(cx: Scope, ctx: &Ctx) {
    let Some((r, scope)) = selected_run(ctx) else {
        ctx.store
            .notice
            .set(Some("no run selected — nothing to steer".into()));
        return;
    };
    if !scope.actionable() {
        ctx.store.notice.set(Some(format!(
            "runs in {} are ticked by that plane's own runtime — cancel/steer from this console cannot reach them",
            scope.short()
        )));
        return;
    }
    open_steer_form(cx, ctx, r);
}

fn runs_table(
    cx: Scope,
    t: &TokenSet,
    data: &[RunRow],
    sel: Signal<usize>,
    expanded: Signal<Option<usize>>,
) -> View {
    // The web Runs table's columns (Run, Workflow, Status, Node, Session,
    // Updated); cells WRAP — a run id is never cut. Enter opens the row's
    // Inspect rows in place; `s` steers, `c` cancels, `i` reads the run.
    let rows: Vec<super::kit::Row> = data
        .iter()
        .map(|r| {
            super::kit::Row::new(vec![
                r.run_id.clone(),
                r.workflow_id.clone(),
                if r.paused {
                    format!("{} (paused)", r.status)
                } else {
                    r.status.clone()
                },
                r.current_node.clone(),
                r.session_id.clone(),
                r.updated_at.chars().take(19).collect(),
            ])
            .detail(
                run_detail_rows(r)
                    .into_iter()
                    .map(|(k, v)| format!("{k:<9}{v}"))
                    .collect(),
            )
        })
        .collect();
    let rules = vec![
        widths::ColRule::tail("Run", 12),
        widths::ColRule::tail("Workflow", 12),
        widths::ColRule::head("Status", 7),
        widths::ColRule::head("Node", 4),
        widths::ColRule::tail("Session", 8),
        widths::ColRule::head("Updated", 19),
    ];
    super::kit::WrapTable::new(rules, rows, sel)
        .expanded(expanded)
        .layout(LayoutStyle::default().grow(1.0).min_h(2))
        .element(cx, t)
        .build()
}

fn confirm_cancel(_cx: Scope, ctx: &Ctx, r: RunRow, confirm: super::kit::InlineConfirm) {
    // Cancel only makes sense on a live run (the web shows no Cancel on a
    // terminal run) — refuse with the reason instead of a no-op command.
    if matches!(r.status.as_str(), "completed" | "failed" | "cancelled") {
        ctx.store.notice.set(Some(format!(
            "run {} is already {} — nothing to cancel",
            r.run_id, r.status
        )));
        return;
    }
    let ctx2 = ctx.clone();
    // The web's confirm, word for word (`cancelRun`), in place.
    confirm.ask(
        format!(
            "Cancel run {}? Any in-flight work stops at the next tick.",
            r.run_id
        ),
        "Cancel run",
        move || {
            ctx2.send(Cmd::CancelRun {
                run_id: r.run_id.clone(),
            })
        },
    );
}

/// Steer: inject guidance into a live run (folds at its next loop
/// boundary). One small form — the guidance text is the whole payload.
fn open_steer_form(cx: Scope, ctx: &Ctx, r: RunRow) {
    if !matches!(r.status.as_str(), "running" | "waiting") {
        ctx.store.notice.set(Some(format!(
            "run {} is {} — only live runs can be steered",
            r.run_id.chars().take(8).collect::<String>(),
            r.status
        )));
        return;
    }
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(74, 14), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let guidance = mcx.signal(String::new());
        let rid = r.run_id.clone();
        let rid_short: String = rid.chars().take(12).collect();
        let ctx3 = ctx2.clone();
        let close2 = close.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Steer run {rid_short} ({})", r.workflow_id),
                t0.accent,
            )]))
            .children(
                super::util::wrap_text(
                    &format!(
                        "Guidance folds into {rid_short}'s next reasoning cycle (durable inbox — delivered at the loop boundary, never lost)."
                    ),
                    68,
                )
                .into_iter()
                .map(|l| line(vec![span(l, t0.text_faint)]))
                .collect::<Vec<_>>(),
            )
            .child(field(
                &t0,
                "guidance",
                TextInput::new()
                    .value(guidance)
                    .placeholder("e.g. stop exploring, finish with what you have")
                    .placeholder_while_focused(true)
                    .layout(LayoutStyle::default().w(56).h(1))
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Send guidance")
                            .on_click(move || {
                                let g = guidance.get_untracked().trim().to_string();
                                if g.is_empty() {
                                    ctx3.store
                                        .notice
                                        .set(Some("type the guidance first".into()));
                                    return;
                                }
                                ctx3.send(Cmd::SteerRun {
                                    run_id: rid.clone(),
                                    guidance: g,
                                });
                                close2();
                            })
                            .element(mcx, &t0)
                            .build(),
                    )
                    .child(
                        Button::new("Cancel (Esc)")
                            .on_click(move || close_cancel())
                            .element(mcx, &t0)
                            .build(),
                    )
                    .build(),
            )
            .build()
    });
}

fn confirm_purge_home(cx: Scope, ctx: &Ctx, home: crate::store::DataHomeRow) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Purge data home '{}' ({} — {})? A dry-run gates the real purge; contents under {} are deleted.",
            home.name, home.kind, home.owner, home.path
        ),
        "Purge it (dry-run first)",
        "Keep it",
        move || ctx2.send(Cmd::PurgeDataHome { name: home.name }),
    );
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
        assert_eq!(got("", "").len(), 2);
    }

    #[test]
    fn the_note_line_names_the_half_that_answered() {
        assert_eq!(query_bit("*.jpg"), "q=\"*.jpg\" (glob)");
        assert_eq!(query_bit("photo"), "q=\"photo\"");
    }

    fn config(v: serde_json::Value) -> RuntimeConfigData {
        RuntimeConfigData::from_value(&v)
    }

    /// Inherited (non-stored) values never prefill the gateway defaults
    /// form, so a save cannot promote them to stored settings — the web's
    /// renderGatewayPolicyModal rule.
    #[test]
    fn workspace_defaults_prefill_only_stored_choices() {
        let inherited = config(json!({
            "writable": true,
            "workspace_root": {"value": "/srv/ws", "source": "env"},
            "trust_client_launch_folder": {"value": true, "source": "default"},
            "client_workspace_scope_overrides": {"value": false, "source": "default"},
            "workspace_allowed_paths": {"value": "/a\n/b", "source": "stored"},
            "workspace_blocked_paths": {"value": "", "source": "default"},
            "workspace_default_mode": {"value": "whitelist", "source": "default"},
        }));
        let pre = WorkspaceDefaults::prefill(&inherited);
        assert_eq!(pre.root, "", "an env root is not a stored choice");
        assert_eq!(pre.trust, None);
        assert_eq!(pre.overrides, None);
        assert_eq!(pre.allowed, "/a\n/b");
        assert_eq!(
            pre.body(),
            json!({
                "workspace_default_mode": "whitelist",
                "workspace_root": null,
                "workspace_allowed_paths": "/a\n/b",
                "workspace_blocked_paths": "",
                "trust_client_launch_folder": null,
            }),
            "inherit = null trust, no bypass key (the web's exact body)"
        );
        let stored = config(json!({
            "workspace_root": {"value": "/srv/ws", "source": "stored"},
            "trust_client_launch_folder": {"value": false, "source": "stored"},
            "client_workspace_scope_overrides": {"value": true, "source": "stored"},
            "workspace_default_mode": {"value": "blacklist", "source": "stored"},
        }));
        let body = WorkspaceDefaults::prefill(&stored).body();
        assert_eq!(body["workspace_root"], "/srv/ws");
        assert_eq!(body["trust_client_launch_folder"], false);
        assert_eq!(body["client_workspace_scope_overrides"], true);
        assert_eq!(body["workspace_default_mode"], "blacklist");
        assert!(
            body.get("user_workspace_policies").is_none(),
            "never names the per-user map"
        );
    }
}
