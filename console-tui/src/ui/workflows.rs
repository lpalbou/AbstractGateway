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
use abstracttui::widgets::{Table, TextInput};

use super::util::{ellipsize, field, line, span, span_bold};
use super::widths;
use super::{open_form, Ctx};
use crate::store::{WorkflowRow, WorkflowsData};
use crate::worker::operator::OpCmd;
use crate::worker::Cmd;

/// The footer verbs of this screen that only an admin may use: archive
/// (one version / every version), import and reload. Listing and export
/// stay open to every principal — the web keeps the tab for everyone.
pub const ADMIN_KEYS: &[&str] = &["d", "D", "i", "L"];

/// The purpose line at the top of the screen (DESIGN-v2 §4).
pub const PURPOSE: &str = "Workflows are the programs your apps and automations run. They come in bundles (.flow files): some ship with the gateway, others you import or publish from AbstractFlow.";
/// The per-app defaults section (DESIGN-v2 §4.2).
pub const DEFAULTS_TITLE: &str = "Default workflow per app";
pub const DEFAULTS_SENTENCE: &str =
    "When an app asks for 'an agent' without naming a workflow, the gateway runs this one.";
/// Said when the gateway's runtime-config rows lack the §6 interface
/// table fields (`label` / `state`): the section refuses to show raw ids.
pub const DEFAULTS_NOT_SERVED: &str = "This gateway sends no plain names for its agent interfaces (label/state missing) — update the gateway. Runtimes (Knobs) still edits the raw settings.";

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, ui.workflow_sel, move || {
        store
            .workflows
            .with(|d| d.ready().map(|w| w.rows.len()).unwrap_or(0))
    });
    // The per-app defaults: selection + the folded "Other workflow types".
    let defaults_sel = cx.signal(0usize);
    let other_open = cx.signal(false);
    super::util::clamp_selection(cx, defaults_sel, move || {
        let open = other_open.get();
        store.runtime_config.with(|d| {
            d.ready()
                .map(|c| visible_defaults(&c.agent_defaults, open).len())
                .unwrap_or(0)
        })
    });
    // The defaults (and the plain "Used by" names) come from the admin
    // runtime-config read: load it once an admin is here.
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let admin = store.conn.with(crate::store::ConnPhase::is_admin);
            if admin && matches!(store.runtime_config.get(), crate::store::Loadable::NotAsked) {
                store.runtime_config.set(crate::store::Loadable::Loading);
                ctx_load.send(Cmd::LoadRuntimeConfig);
            }
        });
    }

    let ctx_table = ctx.clone();
    let ctx_export = ctx.clone();
    let ctx_del_ver = ctx.clone();
    let ctx_del_all = ctx.clone();
    let ctx_refresh = ctx.clone();
    let ctx_drafts = ctx.clone();
    let ctx_import = ctx.clone();
    let ctx_reload = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('r')), {
            let c = ctx_refresh.clone();
            move |_| {
                c.send(Cmd::LoadWorkflows {
                    include_drafts: c.ui.workflow_drafts.get_untracked(),
                });
                if c.store
                    .conn
                    .with_untracked(crate::store::ConnPhase::is_admin)
                {
                    c.store.runtime_config.set(crate::store::Loadable::Loading);
                    c.send(Cmd::LoadRuntimeConfig);
                }
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
        .shortcut(KeyChord::plain(Key::Char('o')), move |_| {
            other_open.update(|v| *v = !*v)
        })
        .shortcut(KeyChord::plain(Key::Char('e')), {
            let c = ctx_export.clone();
            move |_| export_selected(cx, &c)
        })
        .shortcut(KeyChord::plain(Key::Char('d')), {
            let c = ctx_del_ver.clone();
            move |_| archive_selected(cx, &c, false)
        })
        .shortcut(KeyChord::plain(Key::Char('D')), {
            let c = ctx_del_all.clone();
            move |_| archive_selected(cx, &c, true)
        })
        // Web parity: "Import…" (a .flow bundle) and a registry reload.
        .shortcut(KeyChord::plain(Key::Char('i')), move |_| {
            open_import(cx, &ctx_import)
        })
        .shortcut(KeyChord::plain(Key::Char('L')), move |_| {
            reload(&ctx_reload)
        })
        // The purpose line, wrapped to the terminal (never cut).
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |pcx| {
                let w = (abstracttui::app::use_viewport(pcx).get().w - 2).max(20) as usize;
                let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                for l in super::util::wrap_text(PURPOSE, w) {
                    col = col.child(line(vec![span(format!(" {l}"), tt.text_muted)]));
                }
                col.build()
            },
        ))
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Workflows")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .min_h(6)
                        .padding(Edges {
                            left: 1,
                            right: 1,
                            top: 0,
                            bottom: 0,
                        }),
                )
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    let keeper = super::util::FocusKeeper::new();
                    move |gcx| {
                        let conn = store.conn.get();
                        let data = store.workflows.get();
                        let labels = interface_labels(&store.runtime_config.get());
                        let sel = ui.workflow_sel;
                        let _ = &ctx_table;
                        super::util::loadable_view_kept(
                            &keeper,
                            &tt,
                            &conn,
                            || store.tick.get(),
                            &data,
                            |d: &WorkflowsData| d.rows.is_empty() && d.skipped.is_empty(),
                            "no workflows registered on this gateway",
                            |d: &WorkflowsData| {
                                let mut children: Vec<View> =
                                    vec![master_table(gcx, &tt, d, &labels, sel, &keeper)];
                                if let Some(row) = d.rows.get(sel.get()) {
                                    children.push(detail_block(gcx, &tt, row, &labels));
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
                    }
                }))
                .element(t)
                .build(),
        )
        .child(defaults_block(cx, ctx, t, defaults_sel, other_open))
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            let drafts = ui.workflow_drafts.get();
            line(vec![
                span("Tab", tt.accent),
                span(" workflows ⇄ defaults  ", tt.text_muted),
                span("Enter", tt.accent),
                span(" pick a default  ", tt.text_muted),
                span("o", tt.accent),
                span(" other types  ", tt.text_muted),
                span("e", tt.accent),
                span(" export  ", tt.text_muted),
                span("d", tt.accent),
                span(" archive version  ", tt.text_muted),
                span("D", tt.accent),
                span(" archive bundle  ", tt.text_muted),
                span("i", tt.accent),
                span(" import .flow  ", tt.text_muted),
                span("L", tt.accent),
                span(" reload  ", tt.text_muted),
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

/// interface id → plain name, from the runtime-config agent-default rows
/// (the gateway's ONE interface table, DESIGN-v2 §6). Empty when not read.
pub fn interface_labels(
    cfg: &crate::store::Loadable<crate::store::RuntimeConfigData>,
) -> Vec<(String, String)> {
    cfg.ready()
        .map(|c| {
            c.agent_defaults
                .iter()
                .filter_map(|a| a.label.clone().map(|l| (a.interface.clone(), l)))
                .collect()
        })
        .unwrap_or_default()
}

/// The "Used by" cell: the plain names of the interfaces a bundle declares
/// (an interface the table does not name keeps its id); "—" for none.
pub fn used_by_text(interfaces: &[String], labels: &[(String, String)]) -> String {
    if interfaces.is_empty() {
        return "—".to_string();
    }
    interfaces
        .iter()
        .map(|i| {
            labels
                .iter()
                .find(|(id, _)| id == i)
                .map(|(_, l)| l.clone())
                .unwrap_or_else(|| i.clone())
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// The rows the defaults table shows: the "apps" group, then (when the
/// fold is open) the "other" group.
pub fn visible_defaults(
    rows: &[crate::store::AgentDefault],
    other_open: bool,
) -> Vec<crate::store::AgentDefault> {
    let mut out: Vec<_> = rows
        .iter()
        .filter(|a| a.group != "other")
        .cloned()
        .collect();
    if other_open {
        out.extend(rows.iter().filter(|a| a.group == "other").cloned());
    }
    out
}

fn defaults_block(
    cx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    sel: Signal<usize>,
    other_open: Signal<bool>,
) -> View {
    let store = ctx.store;
    let tt = *t;
    let ctx_pick = ctx.clone();
    Block::new()
        .border(BorderKind::Rounded)
        .title(DEFAULTS_TITLE)
        .fill(t.surface)
        .layout(LayoutStyle::column().gap(0).shrink(0.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .child(line(vec![span(DEFAULTS_SENTENCE, tt.text_muted)]))
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |dcx| {
                use crate::store::Loadable;
                if let Some(why) = store
                    .conn
                    .with(|c| c.admin_refusal("the default workflow per app"))
                {
                    return line(vec![span(why, tt.text_muted)]);
                }
                let open = other_open.get();
                match store.runtime_config.get() {
                    Loadable::NotAsked | Loadable::Loading => {
                        line(vec![span("⟳ reading the defaults…", tt.info)])
                    }
                    Loadable::Failed(e) => line(vec![span(
                        format!("couldn't read the defaults: {e} — r retries"),
                        tt.error,
                    )]),
                    Loadable::Ready(c) => {
                        if c.agent_defaults.is_empty() {
                            return line(vec![span(
                                "no app on this gateway asks for an agent workflow",
                                tt.text_muted,
                            )]);
                        }
                        if c.agent_defaults
                            .iter()
                            .any(|a| a.label.is_none() || a.state.is_none())
                        {
                            return line(vec![span(DEFAULTS_NOT_SERVED, tt.error)]);
                        }
                        let rows = visible_defaults(&c.agent_defaults, open);
                        let others = c
                            .agent_defaults
                            .iter()
                            .filter(|a| a.group == "other")
                            .count();
                        let vw = abstracttui::app::use_viewport(dcx).get().w;
                        let mut cells: Vec<Vec<String>> = rows
                            .iter()
                            .map(|a| {
                                let label = a.label.clone().unwrap_or_default();
                                let label = if a.group == "other" {
                                    format!("  {label}")
                                } else {
                                    label
                                };
                                vec![label, a.state_text().unwrap_or_default()]
                            })
                            .collect();
                        let rules = [
                            widths::ColRule::head("app", 16),
                            widths::ColRule::head("runs", 14),
                        ];
                        let cols = widths::columns(&rules, &mut cells, vw - widths::BLOCK_CHROME);
                        let ctx_act = ctx_pick.clone();
                        let rows_act = rows.clone();
                        let h = rows.len() as i32 + 1;
                        let mut col = Element::new()
                            .style(LayoutStyle::column().gap(0).shrink(0.0))
                            .child(
                                Table::new(cols)
                                    .rows(cells)
                                    .selection(sel)
                                    .on_activate(move |i| {
                                        if let Some(a) = rows_act.get(i) {
                                            open_default_picker(cx, &ctx_act, a.clone());
                                        }
                                    })
                                    .layout(LayoutStyle::default().h(h).shrink(0.0))
                                    .element(dcx, &tt)
                                    .build(),
                            );
                        if others > 0 {
                            col = col.child(line(vec![span(
                                format!(
                                    "{} Other workflow types ({others}) — o {}",
                                    if open { "▾" } else { "▸" },
                                    if open { "hides them" } else { "shows them" }
                                ),
                                tt.text_faint,
                            )]));
                        }
                        col.build()
                    }
                }
            },
        ))
        // The selected row's sentence (and a broken row's reason): its own
        // region, so moving the selection never rebuilds the table (the
        // table keeps the keyboard).
        .child(dyn_view_scoped(
            LayoutStyle::column().gap(0).shrink(0.0),
            move |dcx| {
                let open = other_open.get();
                let idx = sel.get();
                if store.conn.with(|c| c.admin_refusal("x").is_some()) {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                }
                let Some(a) = store.runtime_config.with(|c| {
                    c.ready().and_then(|c| {
                        if c.agent_defaults
                            .iter()
                            .any(|a| a.label.is_none() || a.state.is_none())
                        {
                            return None;
                        }
                        visible_defaults(&c.agent_defaults, open).get(idx).cloned()
                    })
                }) else {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                };
                let vw = abstracttui::app::use_viewport(dcx).get().w;
                let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                let w = (vw - widths::BLOCK_CHROME).max(20) as usize;
                if a.state.as_deref() == Some("broken") {
                    let why = if a.reason.is_empty() {
                        "the configured workflow can't run".to_string()
                    } else {
                        a.reason.clone()
                    };
                    for l in super::util::wrap_text(&why, w) {
                        col = col.child(line(vec![span(l, tt.warn)]));
                    }
                }
                let mut help = a.help.clone();
                if let Some(app) = &a.app {
                    if help.is_empty() {
                        help = format!("Asked for by {app}.");
                    }
                }
                for l in super::util::wrap_text(&help, w) {
                    col = col.child(line(vec![span(l, tt.text_faint)]));
                }
                if abstracttui::app::use_viewport(dcx).get().h >= 36 {
                    col = col.child(line(vec![span(
                        ellipsize(&format!("interface {}", a.interface), w),
                        tt.text_faint,
                    )]));
                }

                col.build()
            },
        ))
        .element(t)
        .build()
}

/// Enter on a default: a picker of the entrypoints declaring that
/// interface plus the empty choice; the choice saves at once (no Save).
pub fn open_default_picker(cx: Scope, ctx: &Ctx, a: crate::store::AgentDefault) {
    use abstracttui::app::{ChoiceOutcome, ChoicePrompt};
    if !super::util::admin_gate(&ctx.store, "changing the default workflow per app") {
        return;
    }
    let writable = ctx
        .store
        .runtime_config
        .with_untracked(|c| c.ready().map(|c| c.writable).unwrap_or(false));
    if !writable {
        ctx.store.notice.set(Some(
            "the gateway reports these settings as read-only for this token".into(),
        ));
        return;
    }
    let label = a.label.clone().unwrap_or_else(|| a.interface.clone());
    let empty_label = if a.builtin.is_empty() {
        "Clients choose".to_string()
    } else {
        let n = if a.state.as_deref() == Some("builtin") && !a.name.is_empty() {
            a.name.clone()
        } else {
            a.builtin.clone()
        };
        format!("Built in: {n}")
    };
    let mut prompt = ChoicePrompt::new(format!("{label} — which workflow runs by default?"));
    for (i, (value, name, ver)) in a.eligible_named.iter().enumerate() {
        let current = a.state.as_deref() == Some("set") && a.value == *value;
        prompt = prompt.option_detail(
            i.to_string(),
            format!("{name} {ver}{}", if current { " (current)" } else { "" }),
            value.clone(),
        );
    }
    prompt = prompt.option_detail(
        "none",
        empty_label,
        if a.builtin.is_empty() {
            "the gateway runs nothing by default; each app picks its workflow".to_string()
        } else {
            "back to the gateway's built-in workflow".to_string()
        },
    );
    prompt = prompt.option("keep", "Keep the current choice");
    let ctx2 = ctx.clone();
    let choices: Vec<String> = a.eligible_named.iter().map(|(v, _, _)| v.clone()).collect();
    let iface = a.interface.clone();
    super::open_prompt(cx, ctx.ui, prompt, move |outcome| {
        let ChoiceOutcome::Answered(ans) = outcome else {
            return;
        };
        let Some(pick) = ans.selected.first() else {
            return;
        };
        let value = match pick.as_str() {
            "keep" => return,
            "none" => String::new(),
            i => match i.parse::<usize>().ok().and_then(|i| choices.get(i)) {
                Some(v) => v.clone(),
                None => return,
            },
        };
        ctx2.send(Cmd::SaveRuntimeConfig {
            body: serde_json::json!({ "agents": { "default_workflow": { iface: value } } }).into(),
            form_id: None,
        });
    });
}

fn selected_row(ctx: &Ctx) -> Option<WorkflowRow> {
    let sel = ctx.ui.workflow_sel.get_untracked();
    ctx.store
        .workflows
        .with_untracked(|d| d.ready().and_then(|w| w.rows.get(sel).cloned()))
}

/// Where an export lands by default: the console's own downloads folder
/// (the sandbox's artifacts go there too), never the working directory.
pub fn export_default_path(bundle_id: &str, version: &str) -> String {
    super::sandbox::artifact_dir()
        .join(format!("{bundle_id}@{version}.flow"))
        .display()
        .to_string()
}

/// `e`: export the selected version's ORIGINAL bytes to a LOCAL file. The
/// TUI is frequently not on the gateway's machine, so the file lands on
/// THIS one; the destination is shown (editable) and confirmed first.
fn export_selected(cx: Scope, ctx: &Ctx) {
    let Some(row) = selected_row(ctx) else {
        ctx.store
            .notice
            .set(Some("no workflow selected — nothing to export".into()));
        return;
    };
    let version = row
        .versions
        .first()
        .map(|(v, _, _, _)| v.clone())
        .unwrap_or_default();
    let label = format!("{}@{}", row.bundle_id, version);
    let default_dest = export_default_path(&row.bundle_id, &version);
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(96, 11), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        // The path field fits the dialog as clamped on THIS terminal (a
        // fixed 70 cells ran past the border at 80 columns).
        let input_w =
            (96.min(abstracttui::app::use_viewport(mcx).get_untracked().w - 2) - 26).clamp(20, 70);
        let dest = mcx.signal(default_dest.clone());
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let submit = {
            let ctx_s = ctx2.clone();
            let bundle_id = row.bundle_id.clone();
            let version = version.clone();
            move || {
                if in_flight.get_untracked() {
                    return;
                }
                let d = dest.get_untracked().trim().to_string();
                if d.is_empty() {
                    form_error.set(Some("type where to save the .flow file".into()));
                    return;
                }
                form_error.set(None);
                in_flight.set(true);
                ctx_s.send(Cmd::ExportWorkflow {
                    bundle_id: bundle_id.clone(),
                    version: version.clone(),
                    dest: d,
                    form_id: Some(form_id),
                });
            }
        };
        let submit2 = submit.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(format!("Export {label}"), t0.accent)]))
            .child(line(vec![span(
                "saved on THIS machine; an existing file is never overwritten",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "save to",
                TextInput::new()
                    .value(dest)
                    .layout(LayoutStyle::default().w(input_w).h(1))
                    .on_submit(move |_: &str| submit())
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Export")
                            .on_click(submit2)
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

/// Archive confirms first (web parity, DESIGN-v3 §5.3: workflows are
/// archived, never deleted), defaulting to keep. Shipped workflows refuse
/// (the gateway answers 409 with the reason, shown as the write's error).
fn archive_selected(cx: Scope, ctx: &Ctx, whole_bundle: bool) {
    if !super::util::admin_gate(&ctx.store, "archiving a workflow") {
        return;
    }
    let Some(row) = selected_row(ctx) else {
        ctx.store
            .notice
            .set(Some("no workflow selected — nothing to archive".into()));
        return;
    };
    let version = if whole_bundle {
        String::new()
    } else {
        row.versions
            .first()
            .map(|(v, _, _, _)| v.clone())
            .unwrap_or_default()
    };
    let label = if version.is_empty() {
        row.bundle_id.clone()
    } else {
        format!("{}@{}", row.bundle_id, version)
    };
    let c = ctx.clone();
    let bundle_id = row.bundle_id;
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Archive {label}? It disappears from lists and can't start new runs; \
             the file and every past run stay on the gateway."
        ),
        "Archive",
        "Keep it",
        move || {
            c.send(Cmd::ArchiveWorkflow { bundle_id, version });
        },
    );
}

/// `L`: re-read the bundles folder on the gateway (POST /bundles/reload),
/// then re-list — what "fix the cause and reload" below asks for.
fn reload(ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "reloading workflows") {
        return;
    }
    ctx.send(Cmd::Operator(OpCmd::ReloadWorkflows {
        include_drafts: ctx.ui.workflow_drafts.get_untracked(),
    }));
}

/// `i`: install a `.flow` from a path on THIS machine (the TUI may run
/// elsewhere than the gateway — the bytes are uploaded, like the web's
/// file picker). overwrite=false, reload=true: the web's exact request.
fn open_import(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "importing a workflow") {
        return;
    }
    let ctx2 = ctx.clone();
    open_form(ctx, cx, Size::new(96, 11), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        // The path field fits the dialog as clamped on THIS terminal (a
        // fixed 70 cells ran past the border at 80 columns).
        let input_w =
            (96.min(abstracttui::app::use_viewport(mcx).get_untracked().w - 2) - 26).clamp(20, 70);
        let path = mcx.signal(String::new());
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let form_id = crate::worker::next_form_id();
        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());
        let submit = {
            let ctx_s = ctx2.clone();
            move || {
                if in_flight.get_untracked() {
                    return;
                }
                let p = path.get_untracked();
                if p.trim().is_empty() {
                    form_error.set(Some("type the path of a .flow file on this machine".into()));
                    return;
                }
                form_error.set(None);
                in_flight.set(true);
                ctx_s.send(Cmd::Operator(OpCmd::ImportWorkflow {
                    path: p,
                    include_drafts: ctx_s.ui.workflow_drafts.get_untracked(),
                    form_id: Some(form_id),
                }));
            }
        };
        let submit2 = submit.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Import a workflow bundle (.flow)", t0.accent)]))
            .child(line(vec![span(
                "a file on THIS machine — its bytes are uploaded to the gateway; an existing version is never overwritten",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "file",
                TextInput::new()
                    .value(path)
                    .placeholder("~/Downloads/my-workflow.flow")
                    .layout(LayoutStyle::default().w(input_w).h(1))
                    .on_submit(move |_: &str| submit())
                    .element(mcx, &t0)
                    .autofocus()
                    .build(),
            ))
            .child(super::message_slot(theme, form_error, in_flight))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(Button::new("Import").on_click(submit2).element(mcx, &t0).build())
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

fn master_table(
    cx: Scope,
    t: &TokenSet,
    d: &WorkflowsData,
    labels: &[(String, String)],
    sel: Signal<usize>,
    keeper: &super::util::FocusKeeper,
) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    let wide = vw >= 100;

    let mut rows: Vec<Vec<String>> = d
        .rows
        .iter()
        .map(|r| {
            let name = if r.name.is_empty() {
                r.bundle_id.clone()
            } else {
                r.name.clone()
            };
            let name = if r.deprecated {
                format!("{name} · Deprecated")
            } else {
                name
            };
            let what = if r.description.is_empty() {
                "—".to_string()
            } else {
                r.description.clone()
            };
            let mut row = vec![name, what, r.version_text()];
            if wide {
                row.push(r.source_text().to_string());
                row.push(used_by_text(&r.interfaces, labels));
            }
            row
        })
        .collect();

    let mut rules = vec![
        widths::ColRule::head("name", 14),
        widths::ColRule::head("what it does", 18),
        widths::ColRule::head("version", 14),
    ];
    if wide {
        rules.push(widths::ColRule::head("source", 8));
        rules.push(widths::ColRule::head("used by", 12));
    }

    let cols = widths::columns(&rules, &mut rows, vw - widths::BLOCK_CHROME);
    keeper.wire(
        Table::new(cols)
            .rows(rows)
            .selection(sel)
            .layout(LayoutStyle::default().grow(1.0).min_h(3))
            .element(cx, t),
    )
}

/// The gateway default agent workflows (interface, workflow_id) that run
/// an entrypoint of `bundle_id` (any version).
pub fn agent_default_marks(
    bundle_id: &str,
    agent_defaults: &[(String, String)],
) -> Vec<(String, String)> {
    let prefix = format!("{bundle_id}@");
    agent_defaults
        .iter()
        .filter(|(_, wid)| wid.starts_with(&prefix))
        .cloned()
        .collect()
}

fn detail_block(cx: Scope, t: &TokenSet, row: &WorkflowRow, labels: &[(String, String)]) -> View {
    let vw = abstracttui::app::use_viewport(cx).get().w;
    // A short terminal keeps the table readable: the detail shrinks to
    // the name line and one line of description.
    let tall = abstracttui::app::use_viewport(cx).get().h >= 36;
    let w = (vw - widths::BLOCK_CHROME).max(20) as usize;
    let mut children: Vec<View> = Vec::new();

    children.push(line(vec![
        span_bold(
            if row.name.is_empty() {
                row.bundle_id.clone()
            } else {
                row.name.clone()
            },
            t.accent,
        ),
        span(format!("  bundle {}", row.bundle_id), t.text_faint),
        span(format!("  · {}", row.source_text()), t.text_faint),
    ]));
    // The full description when it is longer than the table cell.
    if !row.description.is_empty() {
        for l in super::util::wrap_text(&row.description, w)
            .into_iter()
            .take(if tall { 3 } else { 0 })
        {
            children.push(line(vec![span(l, t.text_muted)]));
        }
    }

    let rows: Vec<Vec<String>> = row
        .versions
        .iter()
        .map(|(ver, channel, created, eps)| {
            vec![
                ver.clone(),
                channel.clone(),
                created.chars().take(10).collect::<String>(),
                eps.to_string(),
            ]
        })
        .collect();
    let rules = vec![
        widths::ColRule::tail("version", 10),
        widths::ColRule::head("channel", 9),
        widths::ColRule::head("created", 10),
        widths::ColRule::head("entrypoints", 11),
    ];
    if tall {
        children.push(text_table(t, &rules, rows, vw - widths::BLOCK_CHROME));
    }

    for (name, desc, ifaces) in row.entrypoint_info.iter().filter(|_| tall) {
        let mut text = name.clone();
        if !desc.is_empty() {
            text.push_str(&format!(" — {desc}"));
        }
        if !ifaces.is_empty() {
            text.push_str(&format!(" · used by {}", used_by_text(ifaces, labels)));
        }
        children.push(line(vec![span(ellipsize(&text, w), t.text)]));
    }

    let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
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

    let rows: Vec<Vec<String>> = groups
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
    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![
            span_bold("Broken workflows", t.warn),
            span(
                format!(
                    "  {workflows} workflow(s), {versions} version(s) the gateway could not load"
                ),
                t.text_muted,
            ),
        ]))
        .child(if abstracttui::app::use_viewport(cx).get().h >= 36 {
            line(vec![span(
                "the files are still on disk and nothing was deleted — fix the cause and reload, or archive them",
                t.text_faint,
            )])
        } else {
            Element::new().style(LayoutStyle::default().h(0)).build()
        })
        .child(text_table(t, &rules, rows, vw - widths::BLOCK_CHROME))
        .build()
}

/// A read-only grid as plain lines: the same column solver as the tables,
/// but not focusable — Tab moves between the two lists the screen acts on
/// (workflows, defaults), never into a reference grid.
fn text_table(
    t: &TokenSet,
    rules: &[widths::ColRule],
    mut rows: Vec<Vec<String>>,
    rect_w: i32,
) -> View {
    let w = widths::solve(rules, &rows, rect_w);
    widths::fit_cells(rules, &mut rows, &w);
    let fmt = |cells: Vec<String>| {
        cells
            .iter()
            .zip(&w)
            .map(|(c, width)| {
                let pad = (*width as usize).saturating_sub(c.chars().count());
                format!("{c}{}", " ".repeat(pad))
            })
            .collect::<Vec<_>>()
            .join(" ")
    };
    let mut col = Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(line(vec![span(
            fmt(rules.iter().map(|r| r.title.to_string()).collect()),
            t.text_muted,
        )]));
    for r in rows {
        col = col.child(line(vec![span(fmt(r), t.text)]));
    }
    col.build()
}
