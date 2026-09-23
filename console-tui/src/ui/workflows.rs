//! Workflows: the registered workflow registry.
//!
//! Mirrors the web console's Workflows tab. A row is a BUNDLE, not a version —
//! "how many versions does this have" is the question the panel exists to
//! answer, and a count has to be a column. The version cell carries TWO numbers
//! (published / draft) because a single total misrepresents a registry that is
//! majority drafts minted one-per-authoring-run.
//!
//! Versions the gateway REFUSED to serve get their own block with the reason.
//! They are not runnable, so listing them as workflows would be a lie; omitting
//! them is the lie this panel was built to remove, because the file is still on
//! disk and still needs a decision.

use abstracttui::prelude::*;
use abstracttui::widgets::Table;

use super::util::{line, loadable_view, span, span_bold};
use super::widths;
use super::Ctx;
use crate::store::{WorkflowRow, WorkflowsData};
use crate::worker::Cmd;

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, ui.workflow_sel, move || {
        store
            .workflows
            .with(|d| d.ready().map(|w| w.rows.len()).unwrap_or(0))
    });

    let ctx_table = ctx.clone();
    let ctx_export = ctx.clone();
    let ctx_del_ver = ctx.clone();
    let ctx_del_all = ctx.clone();
    let ctx_refresh = ctx.clone();
    let ctx_drafts = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('r')), {
            let c = ctx_refresh.clone();
            move |_| {
                c.send(Cmd::LoadWorkflows {
                    include_drafts: c.ui.workflow_drafts.get_untracked(),
                })
            }
        })
        .shortcut(KeyChord::plain(Key::Char('t')), {
            let c = ctx_drafts.clone();
            move |_| {
                c.ui.workflow_drafts.update(|v| *v = !*v);
                c.send(Cmd::LoadWorkflows {
                    include_drafts: c.ui.workflow_drafts.get_untracked(),
                })
            }
        })
        .shortcut(KeyChord::plain(Key::Char('e')), {
            let c = ctx_export.clone();
            move |_| export_selected(&c)
        })
        .shortcut(KeyChord::plain(Key::Char('d')), {
            let c = ctx_del_ver.clone();
            move |_| delete_selected(&c, false)
        })
        .shortcut(KeyChord::plain(Key::Char('D')), {
            let c = ctx_del_all.clone();
            move |_| delete_selected(&c, true)
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Workflows — every workflow registered on this gateway")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .min_h(6)
                        .padding(Edges::all(1)),
                )
                .child(dyn_view_scoped(
                    LayoutStyle::default().grow(1.0),
                    move |gcx| {
                        let conn = store.conn.get();
                        let data = store.workflows.get();
                        let sel = ui.workflow_sel;
                        let _ = &ctx_table;
                        loadable_view(
                            &tt,
                            &conn,
                            || store.tick.get(),
                            &data,
                            |d: &WorkflowsData| d.rows.is_empty() && d.skipped.is_empty(),
                            "no workflows registered on this gateway",
                            |d: &WorkflowsData| {
                                let mut children: Vec<View> = vec![master_table(gcx, &tt, d, sel)];
                                if let Some(row) = d.rows.get(sel.get()) {
                                    children.push(detail_block(
                                        gcx,
                                        &tt,
                                        row,
                                        &d.default_bundle_id,
                                    ));
                                }
                                if !d.skipped.is_empty() {
                                    children.push(skipped_block(gcx, &tt, d));
                                }
                                let mut col =
                                    Element::new().style(LayoutStyle::column().gap(0).grow(1.0));
                                for child in children {
                                    col = col.child(child);
                                }
                                col.build()
                            },
                        )
                    },
                ))
                .element(t)
                .build(),
        )
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let drafts = ui.workflow_drafts.get();
            line(vec![
                span("e", tt.accent),
                span(" export  ", tt.text_muted),
                span("d", tt.accent),
                span(" delete version  ", tt.text_muted),
                span("D", tt.accent),
                span(" delete bundle  ", tt.text_muted),
                span("t", tt.accent),
                span(
                    if drafts {
                        " drafts shown  "
                    } else {
                        " drafts hidden  "
                    },
                    tt.text_muted,
                ),
                span("r", tt.accent),
                span(" refresh", tt.text_muted),
            ])
        }))
        .build()
}

fn selected_row(ctx: &Ctx) -> Option<WorkflowRow> {
    let sel = ctx.ui.workflow_sel.get_untracked();
    ctx.store
        .workflows
        .with_untracked(|d| d.ready().and_then(|w| w.rows.get(sel).cloned()))
}

fn export_selected(ctx: &Ctx) {
    let Some(row) = selected_row(ctx) else { return };
    let version = row
        .versions
        .first()
        .map(|(v, _, _, _)| v.clone())
        .unwrap_or_default();
    // A LOCAL path: the TUI is frequently not on the machine running the
    // gateway, so writing "somewhere on the server" would be an export the
    // operator cannot find.
    let dest = format!("./{}@{}.flow", row.bundle_id, version);
    ctx.send(Cmd::ExportWorkflow {
        bundle_id: row.bundle_id,
        version,
        dest,
    });
}

fn delete_selected(ctx: &Ctx, whole_bundle: bool) {
    let Some(row) = selected_row(ctx) else { return };
    let version = if whole_bundle {
        String::new()
    } else {
        row.versions
            .first()
            .map(|(v, _, _, _)| v.clone())
            .unwrap_or_default()
    };
    ctx.send(Cmd::DeleteWorkflow {
        bundle_id: row.bundle_id,
        version,
    });
}

fn master_table(cx: Scope, t: &TokenSet, d: &WorkflowsData, sel: Signal<usize>) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 100;
    let default_id = d.default_bundle_id.clone();

    let mut rows: Vec<Vec<String>> = d
        .rows
        .iter()
        .map(|r| {
            // TWO numbers, always: "4 pub" alone would hide 15 drafts.
            let versions = if r.draft > 0 {
                format!("{} pub · {} draft", r.published, r.draft)
            } else {
                format!("{} pub", r.published)
            };
            let latest = if r.latest.is_empty() {
                "— (draft only)".to_string()
            } else {
                r.latest.clone()
            };
            let mut status: Vec<&str> = Vec::new();
            if r.bundle_id == default_id {
                status.push("default");
            }
            if r.deprecated {
                status.push("deprecated");
            }
            let mut row = vec![r.bundle_id.clone(), versions, latest];
            if wide {
                row.push(r.entrypoints.to_string());
                row.push(if r.scope == "private" {
                    "registry".to_string()
                } else {
                    r.scope.clone()
                });
            }
            row.push(if status.is_empty() {
                "—".to_string()
            } else {
                status.join(" ")
            });
            row
        })
        .collect();

    let mut rules = vec![
        widths::ColRule::tail("workflow", 18),
        widths::ColRule::head("versions", 14),
        widths::ColRule::head("latest", 10),
    ];
    if wide {
        rules.push(widths::ColRule::head("eps", 4));
        rules.push(widths::ColRule::head("scope", 8));
    }
    rules.push(widths::ColRule::head("status", 10));

    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    Table::new(cols)
        .rows(rows)
        .selection(sel)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .autofocus()
        .build()
}

fn detail_block(cx: Scope, t: &TokenSet, row: &WorkflowRow, default_id: &str) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let mut children: Vec<View> = Vec::new();

    let mut head = vec![
        span_bold(row.bundle_id.clone(), t.accent),
        span("  ", t.text_muted),
        span(
            format!("{} published, {} draft", row.published, row.draft),
            t.text_muted,
        ),
    ];
    if row.bundle_id == default_id {
        head.push(span("  ", t.text_muted));
        head.push(span("default", t.accent));
    }
    children.push(line(head));

    let mut rows: Vec<Vec<String>> = row
        .versions
        .iter()
        .map(|(ver, channel, created, eps)| {
            vec![
                ver.clone(),
                channel.clone(),
                created.chars().take(19).collect::<String>(),
                eps.to_string(),
            ]
        })
        .collect();
    let rules = vec![
        widths::ColRule::tail("version", 16),
        widths::ColRule::head("channel", 9),
        widths::ColRule::head("created", 19),
        widths::ColRule::head("eps", 4),
    ];
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    children.push(Table::new(cols).rows(rows).element(cx, t).build());

    if !row.flows.is_empty() {
        let joined = row
            .flows
            .iter()
            .map(|(flow, _, ifaces)| {
                if ifaces.is_empty() {
                    flow.clone()
                } else {
                    format!("{flow} [{ifaces}]")
                }
            })
            .collect::<Vec<_>>()
            .join("  ");
        children.push(line(vec![
            span("entrypoints: ", t.text_muted),
            span(joined, t.text),
        ]));
    }

    let mut col = Element::new().style(LayoutStyle::column().gap(0));
    for child in children {
        col = col.child(child);
    }
    col.build()
}

fn skipped_block(cx: Scope, t: &TokenSet, d: &WorkflowsData) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;

    // GROUP BY (workflow, reason). Nine versions of one bundle failing for one
    // reason is ONE problem, and printing it nine times buries the fact that it
    // is one problem — the first operator to see this panel could not tell what
    // it was. One row per distinct cause, with how many versions it covers.
    let mut groups: Vec<(String, String, usize)> = Vec::new();
    for s in &d.skipped {
        match groups
            .iter_mut()
            .find(|(b, r, _)| *b == s.bundle_id && *r == s.reason)
        {
            Some((_, _, n)) => *n += 1,
            None => groups.push((s.bundle_id.clone(), s.reason.clone(), 1)),
        }
    }

    let versions: usize = d.skipped.len();
    let workflows: usize = {
        let mut ids: Vec<&str> = d.skipped.iter().map(|s| s.bundle_id.as_str()).collect();
        ids.sort_unstable();
        ids.dedup();
        ids.len()
    };

    let mut rows: Vec<Vec<String>> = groups
        .iter()
        .map(|(bundle, reason, n)| {
            vec![
                bundle.clone(),
                if *n == 1 {
                    "1 version".to_string()
                } else {
                    format!("{n} versions")
                },
                reason.clone(),
            ]
        })
        .collect();
    let rules = vec![
        widths::ColRule::tail("workflow", 18),
        widths::ColRule::head("affected", 11),
        widths::ColRule::head("why the gateway cannot run it", 30),
    ];
    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(line(vec![
            span_bold("Broken workflows", t.warn),
            span(
                format!(
                    "  {workflows} workflow(s), {versions} version(s) the gateway could not load"
                ),
                t.text_muted,
            ),
        ]))
        .child(line(vec![span(
            "the files are still on disk and nothing was deleted — fix the cause and reload, or remove them deliberately",
            t.text_faint,
        )]))
        .child(Table::new(cols).rows(rows).element(cx, t).build())
        .build()
}
