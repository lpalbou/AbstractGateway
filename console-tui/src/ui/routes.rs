//! Multimodal capability routes: the default-vs-override table.
//!
//! The fabricated-selection law (2026-07-17 incident) is implemented
//! here: route editors have an explicit Default-vs-Override mode,
//! placeholder items occupy index 0 of every picker, pick controls are
//! disabled in default mode, the resolved state renders as an
//! "Applies now: …" line derived only from server state, and switching
//! provider resets the model picker — never a fabricated pair.

use abstracttui::prelude::*;
use serde_json::{json, Value};

use super::util::{line, or_dash, span, span_bold};
use super::w::action::{button, On};
use super::w::{Action, Cell, Col, ColW, DataTable, Row as WRow};
use super::widths;
use super::Ctx;
use crate::api::firstrun::{can_download_all, GroupStatus};
use crate::store::{ConnPhase, Loadable, RouteRow, RoutesData, WeightsRow};
use crate::worker::Cmd;

/// The footer verbs of this screen that only an admin may use (the
/// gateway's admin routes: model downloads, their cancel, and
/// apply-recommended). Editing and clearing a route stay open to every
/// principal, like the web's Configure / Clear.
pub const ADMIN_KEYS: &[&str] = &["w", "a", "m", "D", "C"];

/// The route keys apply-recommended plans — AbstractCore's
/// `RECOMMENDED_SELECTORS` (config/capability_defaults.py: text, voice,
/// stt, image, video; `stt` is speech input, `input.voice`). A task row
/// (`output.image.text_to_image`) is not among them: `a` never replaces
/// it, only an edit does.
pub const RECOMMENDED_ROUTE_KEYS: [&str; 5] = [
    "input.text",
    "output.voice",
    "input.voice",
    "output.image",
    "output.video",
];

/// What fixes a configured route this computer cannot run — the truth
/// per row and per principal: `a` → "Replace mine too" only for a key the
/// recommendation plans, an edit for any other row, and nothing a
/// non-admin could do themselves.
pub fn broken_route_fix(key: &str, admin: bool) -> &'static str {
    match (admin, RECOMMENDED_ROUTE_KEYS.contains(&key)) {
        (false, _) => "an admin can change it",
        (true, true) => "a, then Replace mine too, swaps in what runs here (or clears it)",
        (true, false) => "not part of the recommendation: Enter edits it",
    }
}

// ---------------------------------------------------------------------------
// R15 Multimodal (DESIGN-TUI.md §3.10): the web's head (title, subtitle,
// [Apply recommended] [↻ Refresh]), the scope sentence, the weights banner
// with [⤓ Download missing], ONE table (Route · Capability · Provider ·
// Model · Weights · Source · Status · Actions) whose row actions are the
// web's icon buttons with the web's tooltips ("Edit <key>", "Clear <key>",
// "Download <artifact> with <provider>", "Copy: <command>"), the Weights
// cell a pill whose tooltip is the probe's words. Every action is a click
// AND a key; the editor is one FormModal ("Configure capability default").
// ---------------------------------------------------------------------------

/// The page title and subtitle (the web's tab heading).
pub const TITLE: &str = "Multimodal Capabilities";
pub const SUBTITLE: &str = "Which provider/model serves each capability route";
/// The scope sentence under the head (admin / everyone else).
pub const SCOPE_ADMIN: &str = "Editing as admin changes the Gateway multimodal capability defaults. Users inherit these unless they set their own runtime defaults.";
pub const SCOPE_USER: &str = "Editing here changes your runtime multimodal capability defaults. Unset routes inherit the Gateway defaults.";
/// The head buttons' tooltips (the web's `title`).
pub const APPLY_TIP: &str = "Set the recommended provider/model on the text, voice, transcription, image and video routes this computer can run. Routes you configured differently are kept.";
pub const REFRESH_TIP: &str = "Reload providers and capability defaults";
/// The empty table.
pub const EMPTY: &str = "No capability routes were returned by Gateway.";

/// The head actions (single source for the buttons, keys, hints, tests).
pub fn head_actions(admin: bool) -> Vec<Action> {
    let mut out = Vec::new();
    // Admin-only server-side (it rewrites the gateway's store): the web
    // hides the button from everyone else; its key still says why.
    if admin {
        out.push(
            Action::label("apply", "Apply recommended")
                .key('a')
                .tooltip(APPLY_TIP),
        );
    }
    out.push(
        Action::label("refresh", format!("{} Refresh", glyph("rotate")))
            .key('r')
            .tooltip(REFRESH_TIP),
    );
    out
}

fn glyph(id: &str) -> &'static str {
    super::w::glyphs::glyph(id, true)
}

/// The web's `weightView`: the pill's label and tone, and whether a
/// download can do something.
pub fn weight_view(w: &WeightsRow) -> (&'static str, WeightTone, bool) {
    match w.status.as_str() {
        "installed" => ("installed", WeightTone::Ok, false),
        "absent" => ("not downloaded", WeightTone::Warn, w.downloadable),
        "not_applicable" => ("remote", WeightTone::Info, false),
        "unknown" if w.downloadable => ("download needed", WeightTone::Warn, true),
        "unknown" => ("not checked", WeightTone::Muted, false),
        _ => ("unknown", WeightTone::Muted, false),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WeightTone {
    Ok,
    Warn,
    Info,
    Muted,
}

/// The pill's tooltip (R14-W7): AbstractCore's one-sentence `summary`
/// (the words the web prints under the pill), then everything the probe
/// said, one fact per line (the web's `weightTip`: its detail, "To fix:").
pub fn weight_tip(w: &WeightsRow) -> String {
    let mut lines = Vec::new();
    let sum = w.summary.trim();
    if !sum.is_empty() {
        lines.push(sum.to_string());
    }
    let d = w.detail.trim();
    if !d.is_empty() {
        lines.push(if d.ends_with(['.', '!', '?']) {
            d.to_string()
        } else {
            format!("{d}.")
        });
    }
    if !w.instruction.trim().is_empty() {
        lines.push(format!("To fix: {}", w.instruction.trim()));
    }
    lines.join("\n")
}

/// The web's `defaultRowReadOnly`.
pub fn read_only(r: &RouteRow) -> bool {
    r.read_only || r.derived_from.is_some() || (r.covered_by.is_some() && !r.overrideable)
}

/// The web's `defaultRowActionLabel`.
pub fn action_label(r: &RouteRow) -> String {
    if r.covered_by.as_deref() == Some("input.text") {
        return if r.overrideable {
            "Override".into()
        } else {
            "Covered by input.text".into()
        };
    }
    if r.derived_from.as_deref() == Some("input.text") {
        return "Derived \u{2190} input.text".into();
    }
    if r.configured {
        return "Edit".into();
    }
    if r.is_task_parent() {
        "Set for all".into()
    } else {
        "Configure".into()
    }
}

/// The web's `defaultRowStatus` label.
pub fn status_label(r: &RouteRow) -> String {
    let has_pair = r.provider.as_deref().is_some_and(|p| !p.is_empty())
        && r.model.as_deref().is_some_and(|m| !m.is_empty());
    if r.covered_by.as_deref() == Some("input.text") {
        return "covered by input.text".into();
    }
    if let Some(from) = r.derived_from.as_deref() {
        return if has_pair {
            format!("derived \u{2190} {from}")
        } else {
            "not configured".into()
        };
    }
    if let Some(by) = r.covered_by.as_deref() {
        return format!("covered by {by}");
    }
    if r.configured && r.route_unavailable.is_some() {
        return "cannot run here".into();
    }
    if r.configured && r.engine_missing.is_some() {
        return "engine missing".into();
    }
    if r.configured {
        return "configured".into();
    }
    if r.covered_by_tasks {
        return "not needed".into();
    }
    if r.inherits_broad {
        return format!(
            "inherited \u{2190} {}",
            r.broad_key.clone().unwrap_or_default()
        );
    }
    "not configured".into()
}

/// The web's `defaultSourceLabel` (covered / derived rows: "Text Input").
pub fn source_label(r: &RouteRow) -> String {
    if r.covered_by.as_deref() == Some("input.text")
        || r.derived_from.as_deref() == Some("input.text")
    {
        return "Text Input".into();
    }
    match r.source.trim() {
        "abstractcore.runtime" => "Runtime override".into(),
        "abstractcore.gateway_runtime" => "Gateway baseline".into(),
        "abstractcore.local" => "Local Core config".into(),
        "abstractcore.server" => "Core server".into(),
        "abstractcore.capability_defaults" => "Core config".into(),
        "abstractcore.capability_defaults.input_text_multimodal" => "Text model handles it".into(),
        "not_configured" => "Not configured".into(),
        other => other.to_string(),
    }
}

/// A row's actions, in the web's order with the web's tooltips: Edit /
/// Configure (or the read-only words, faint), Clear (configured rows),
/// Download (weights missing and this host can fetch them) or Copy (no
/// download verb here: the command to run). `downloading`: a download of
/// this row's model runs (the Weights cell shows it; no button).
pub fn row_actions(
    r: &RouteRow,
    weights: Option<&WeightsRow>,
    downloading: bool,
    admin: bool,
) -> Vec<Action> {
    let key = &r.key;
    let mut out = Vec::new();
    let label = action_label(r);
    if read_only(r) {
        // Status is not a verb (the web): a read-only row offers no edit;
        // its words stand in the Actions cell (`readonly_words`).
    } else if r.configured && r.covered_by.is_none() {
        out.push(
            Action::glyph("edit", label.clone())
                .key('e')
                .tooltip(format!("{label} {key}")),
        );
    } else {
        out.push(
            Action::glyph("configure", label.clone())
                .key('e')
                .tooltip(format!("{label} {key}")),
        );
    }
    if r.configured && r.covered_by.is_none() && !read_only(r) {
        out.push(
            Action::glyph("clear", "Clear")
                .key('x')
                .tooltip(format!("Clear {key}"))
                .danger(),
        );
    }
    if let Some(w) = weights {
        let (_, _, can) = weight_view(w);
        if !downloading && can {
            let provider = r.provider.clone().unwrap_or_else(|| w.provider.clone());
            let mut a = Action::glyph("install", "Download")
                .key('w')
                .tooltip(format!("Download {} with {provider}", w.artifact));
            a.id = "download";
            if !admin {
                a = a.refused(Some(
                    "Only an admin can download models (they use the gateway host's disk).".into(),
                ));
            }
            out.push(a);
        } else if !downloading && w.status == "absent" && !w.instruction.trim().is_empty() {
            out.push(
                Action::glyph("copy", "Copy")
                    .key('c')
                    .tooltip(format!("Copy: {}", w.instruction.trim())),
            );
        }
    }
    out
}

/// The muted words a read-only row shows where its buttons would be (the
/// web's `defaultRowActionLabel`), with why it cannot be edited.
pub fn readonly_words(r: &RouteRow) -> Option<(String, String)> {
    if !read_only(r) {
        return None;
    }
    let key = &r.key;
    let why = if let Some(d) = &r.derived_from {
        format!("{key} derives from {d} — edit that route instead")
    } else if r.covered_by.is_some() {
        format!("{key} is covered and not overrideable")
    } else {
        format!("{key} is read-only")
    };
    Some((action_label(r), why))
}

/// The page width the content lays out in.
fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// The rows' keys in table order (the selection's single source).
fn route_keys(store: &crate::store::Store) -> Vec<String> {
    store.routes.with(|d| {
        d.ready()
            .map(|d| d.rows.iter().map(|r| r.key.clone()).collect())
            .unwrap_or_default()
    })
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    // The web's second pass: after a non-forced apply that kept routes or
    // left one that cannot run here, offer the forced apply under the
    // web's own label ("Replace mine too" / "Clear what cannot run here").
    {
        let ctx_f = ctx.clone();
        cx.effect(move || {
            let Some(label) = store.apply_followup.get() else {
                return;
            };
            store.apply_followup.set(None);
            let ctx_go = ctx_f.clone();
            let title = if label == "Replace mine too" {
                "The recommended routes were applied; routes you configured were kept."
            } else {
                "The recommended routes were applied; a configured route this computer cannot run was left in place."
            };
            super::w::Confirm::danger(title, &label, "Leave them as they are").open(
                cx,
                ctx_f.ui,
                move || ctx_go.send(Cmd::ApplyRecommendedRoutes { force: true }),
            );
        });
    }

    // `w` / ⤓: the worker read the target's catalog size; now confirm it.
    {
        let ctx_d = ctx.clone();
        cx.effect(move || {
            let Some(offer) = store.download_offer.get() else {
                return;
            };
            store.download_offer.set(None);
            let ctx_go = ctx_d.clone();
            let (provider, artifact) = (offer.provider.clone(), offer.artifact.clone());
            super::w::Confirm::plain(offer.prompt(), "Download", "Not now").open(
                cx,
                ctx_d.ui,
                move || {
                    // The weights column and the voice lists are re-read when
                    // the job FINISHES (worker `finish_download`), not now.
                    ctx_go.send(Cmd::DownloadModel {
                        provider: provider.clone(),
                        artifact: artifact.clone(),
                    });
                },
            );
        });
    }

    super::util::clamp_selection(cx, ui.route_sel, move || {
        store
            .routes
            .with(|d| d.ready().map(|d| d.rows.len()).unwrap_or(0))
    });
    // The table's keyed selection, synced both ways with the legacy index
    // every other path reads (`selected_route`).
    let sel_key = cx.signal(Option::<String>::None);
    cx.effect(move || {
        let k = sel_key.get();
        let keys = route_keys(&store);
        if let Some(i) = k.and_then(|k| keys.iter().position(|x| *x == k)) {
            if ui.route_sel.get_untracked() != i {
                ui.route_sel.set(i);
            }
        }
    });
    cx.effect(move || {
        let i = ui.route_sel.get();
        let keys = route_keys(&store);
        if let Some(k) = keys.get(i) {
            if sel_key.with_untracked(|c| c.as_deref() != Some(k.as_str())) {
                sel_key.set(Some(k.clone()));
            }
        }
    });

    let ctx_edit = ctx.clone();
    let ctx_edit2 = ctx.clone();
    let ctx_clear = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            edit_selected(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Enter), move |_| {
            edit_selected(cx, &ctx_edit2);
        })
        .shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            clear_selected(cx, &ctx_clear);
        })
        // `w` for WEIGHTS, and deliberately not `d` (`d` deletes two
        // screens over; a download must not train that reflex).
        .shortcut(KeyChord::plain(Key::Char('w')), {
            let ctx_dl = ctx.clone();
            move |_| download_selected(&ctx_dl)
        })
        .shortcut(KeyChord::plain(Key::Char('c')), {
            let ctx_cp = ctx.clone();
            move |_| copy_selected(&ctx_cp)
        })
        .shortcut(KeyChord::plain(Key::Char('a')), {
            let ctx_apply = ctx.clone();
            move |_| apply_recommended(cx, &ctx_apply)
        })
        .shortcut(KeyChord::plain(Key::Char('m')), {
            let ctx_m = ctx.clone();
            move |_| download_missing(cx, &ctx_m)
        })
        .shortcut(KeyChord::plain(Key::Char('D')), {
            let ctx_all = ctx.clone();
            move |_| download_all(cx, &ctx_all)
        })
        .shortcut(KeyChord::plain(Key::Char('C')), {
            let ctx_cancel = ctx.clone();
            move |_| cancel_download_all(cx, &ctx_cancel)
        })
        .shortcut(KeyChord::plain(Key::Char('p')), {
            let ctx_plan = ctx.clone();
            move |_| open_plan(cx, &ctx_plan)
        })
        .child(head(cx, ctx, &tt))
        // The scope sentence and the route store's state (read-only and
        // its errors are said; a writable store needs no words beyond
        // "writable").
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |scx| {
                let t = tt;
                let w = page_w(scx);
                let admin = store.conn.with(ConnPhase::is_admin);
                let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                col = col.child(super::w::form::sentence(
                    &t,
                    if admin { SCOPE_ADMIN } else { SCOPE_USER },
                    w,
                    t.text_muted,
                ));
                if let Loadable::Ready(d) = store.routes.get() {
                    let spans = if d.writable {
                        vec![span("writable", t.ok)]
                    } else {
                        vec![
                            span_bold("read-only (backend unreachable?)", t.warn),
                            span(format!("  ·  route store {}", d.authority), t.text_faint),
                        ]
                    };
                    col = col.child(line(spans));
                    if !d.ok {
                        col = col.child(super::w::form::sentence(
                            &t,
                            &format!("gateway reports errors: {}", d.errors.join(" | ")),
                            w,
                            t.error,
                        ));
                    }
                }
                col.build()
            },
        ))
        // WEIGHTS BANNER: only the routes with NOTHING serving them (the
        // gateway's `recommended.gaps`), the web's sentence, and the web's
        // [⤓ Download missing]. A live download outranks it.
        .child(banner(cx, ctx, &tt))
        // RECOMMENDED FOR THIS COMPUTER (the web guide's model step): the
        // Download-all progress while it exists, and the plan summary with
        // its buttons.
        .child(plan_region(cx, ctx, &tt))
        // TRANSCRIPTION (the web's Multimodal card, item 1).
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |lcx| {
                let t = tt;
                let found = store
                    .routes
                    .with(|d| d.ready().and_then(|d| transcription_line(&d.rows)));
                let Some((text, level)) = found else {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                };
                let ink = match level {
                    TranscriptionLevel::Ready => t.ok,
                    TranscriptionLevel::Unset => t.text_muted,
                    TranscriptionLevel::Warn => t.warn,
                    TranscriptionLevel::Error => t.error,
                };
                let w = (page_w(lcx) - 14).max(20) as usize;
                let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
                for (i, l) in super::util::wrap_text(&text, w).into_iter().enumerate() {
                    let lead = if i == 0 {
                        span_bold("Transcription ", t.text)
                    } else {
                        span(" ".repeat(14), t.text)
                    };
                    col = col.child(line(vec![lead, span(l, ink)]));
                }
                col.build()
            },
        ))
        .child(table_region(cx, ctx, &tt, sel_key))
        // SELECTED-ROW LINE: the full route key and, in words, what the
        // row is FOR or why it cannot run.
        .child(dyn_view_scoped(
            LayoutStyle::column().shrink(0.0),
            move |rcx| {
                let t = tt;
                let row: Option<RouteRow> = store.routes.with(|d| {
                    d.ready()
                        .and_then(|d| d.rows.get(ui.route_sel.get()).cloned())
                });
                let Some(r) = row else {
                    return Element::new().style(LayoutStyle::default().h(0)).build();
                };
                let mut text = String::new();
                let mut ink = t.text_muted;
                if let Some(u) = r.route_unavailable.as_ref().filter(|_| r.configured) {
                    let admin = store.conn.with(|c| c.is_admin());
                    text = format!(
                        "configured but cannot run on this computer: {} — {}",
                        u.reason,
                        broken_route_fix(&r.key, admin)
                    );
                    ink = t.error;
                } else if let Some(m) = r.engine_missing.as_ref().filter(|_| r.configured) {
                    text = format!(
                        "{}{}",
                        m.text(),
                        if m.engine_row.is_some() {
                            " (Providers tab, Local providers: Install)"
                        } else {
                            ""
                        }
                    );
                    ink = t.warn;
                } else if let Some(u) = &r.recommendation_unavailable {
                    let what = u.pair_text();
                    text = if what.is_empty() {
                        format!("nothing recommended runs on this computer: {}", u.reason)
                    } else {
                        format!(
                            "the recommended {what} cannot run on this computer: {}",
                            u.reason
                        )
                    };
                    ink = t.warn;
                } else if r.is_task_parent() {
                    text = format!("serves any {} task with no row of its own", r.modality);
                    if r.covered_by_tasks {
                        text.push_str(&format!(
                            " · all {} task rows below are set, so nothing reads it",
                            r.task_keys.len()
                        ));
                    }
                } else if let Some(parent) = &r.broad_key {
                    text = if r.inherits_broad {
                        format!("no value of its own — {parent} answers it")
                    } else {
                        format!("overrides {parent}")
                    };
                }
                // The Weights pill's sentence (core `summary`), readable from
                // the keyboard too (the pill's tooltip carries the rest).
                if let Some(sum) = store.availability.with(|a| {
                    a.ready()
                        .and_then(|a| a.by_route.get(&r.key))
                        .map(|w| w.summary.trim().to_string())
                        .filter(|x| !x.is_empty())
                }) {
                    if text.is_empty() {
                        text = format!("Weights: {sum}");
                    } else {
                        text = format!("{text} · Weights: {sum}");
                    }
                }
                let w = page_w(rcx);
                let head = format!(" {} ", r.key);
                let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
                let lines =
                    super::w::paint::wrap(&text, (w - abstracttui::text::width(&head) - 1).max(10));
                if lines.is_empty() {
                    col = col.child(line(vec![span_bold(head.clone(), t.accent)]));
                }
                for (i, l) in lines.into_iter().enumerate() {
                    let lead = if i == 0 {
                        span_bold(head.clone(), t.accent)
                    } else {
                        span(" ".repeat(abstracttui::text::width(&head) as usize), t.text)
                    };
                    col = col.child(line(vec![lead, span(format!(" {l}"), ink)]));
                }
                col.build()
            },
        ))
        .build()
}

/// Title + subtitle on the left, the web's head buttons on the right
/// (wrapping under the title on a narrow page). Actions open on the PAGE
/// scope (`pcx`).
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let admin = ctx.store.conn.with(ConnPhase::is_admin);
        let w = page_w(hcx);
        let mut row = Element::new().style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .gap(1)
                .shrink(0.0),
        );
        let mut bw = 0;
        for a in head_actions(admin) {
            bw += a.width() + 1;
            let c = ctx.clone();
            let id = a.id;
            row = row.child(button(hcx, &tt, &a, On::Page, true, move || {
                head_action(pcx, &c, id)
            }));
        }
        let title_w = abstracttui::text::width(TITLE).max(abstracttui::text::width(SUBTITLE));
        let side = title_w + bw + 2 <= w;
        let titles = Element::new()
            .style(if side {
                LayoutStyle::column()
                    .width(Dimension::Cells(w - bw - 1))
                    .shrink(0.0)
            } else {
                LayoutStyle::column().shrink(0.0)
            })
            .child(super::w::paint::fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![super::w::Ink::new(TITLE, tt.text).bold()],
                None,
            ))
            .child(super::w::form::sentence(
                &tt,
                SUBTITLE,
                (w - if side { bw + 2 } else { 0 }).max(20),
                tt.text_muted,
            ))
            .build();
        Element::new()
            .style(if side {
                LayoutStyle::row().shrink(0.0)
            } else {
                LayoutStyle::column().shrink(0.0)
            })
            .child(titles)
            .child(row.build())
            .build()
    })
}

/// A head button (or its key).
fn head_action(cx: Scope, ctx: &Ctx, id: &str) {
    match id {
        "apply" => apply_recommended(cx, ctx),
        "refresh" => ctx.refresh_screen(crate::ui::SCREEN_ROUTES),
        _ => {}
    }
}

/// The weights banner's buttons (Download missing, while the gateway names
/// gaps), and the plan region's (the plan, Cancel downloads while a
/// Download all runs). Single source for the buttons, keys and tests.
pub fn banner_actions(admin: bool) -> Vec<Action> {
    // Downloads are admin-only (the gateway host's disk): hidden from
    // everyone else, like the web's admin controls.
    if !admin {
        return Vec::new();
    }
    vec![Action::label(
        "download_missing",
        format!("{} Download missing", glyph("install")),
    )
    .key('m')
    .tooltip(
        "Download the recommended models of the routes that have no model yet (one download each)",
    )]
}

pub fn plan_actions(admin: bool, group_running: bool, can_all: bool) -> Vec<Action> {
    let mut out = vec![Action::label("plan", "Recommended for this computer")
        .key('p')
        .tooltip("Every recommended model for this computer: status, fit warnings, the engine to install")];
    if admin && can_all && !group_running {
        out.push(
            Action::label("download_all", "Download all")
                .key('D')
                .tooltip("Download the whole recommended set in one job"),
        );
    }
    if admin && group_running {
        out.push(
            Action::label("cancel_all", "Cancel downloads")
                .key('C')
                .tooltip("Cancel Download all: every model still downloading stops")
                .danger(),
        );
    }
    out
}

fn banner(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |bcx| {
        let t = tt;
        let store = ctx.store;
        let w = page_w(bcx);
        if let Some(dl) = store.download.get() {
            let tone = if dl.running() {
                t.info
            } else if dl.status == "completed" {
                t.ok
            } else {
                t.error
            };
            return super::w::form::sentence(&t, &format!("download: {}", dl.line()), w, tone);
        }
        match store.availability.get() {
            Loadable::Ready(a) if !a.missing.is_empty() => {
                let routes = a
                    .missing
                    .iter()
                    .map(|(route, _, _)| route.as_str())
                    .filter(|route| !route.is_empty())
                    .collect::<Vec<_>>()
                    .join(", ");
                let pairs = a
                    .missing
                    .iter()
                    .map(|(_, p, art)| format!("{p} {art}"))
                    .collect::<Vec<_>>()
                    .join(", ");
                let text = gaps_sentence(a.missing.len(), &routes, &pairs);
                let admin = store.conn.with(ConnPhase::is_admin);
                let mut row = Element::new().style(
                    LayoutStyle::row()
                        .height(Dimension::Cells(1))
                        .gap(1)
                        .shrink(0.0),
                );
                for a in banner_actions(admin) {
                    let c = ctx.clone();
                    let id = a.id;
                    row = row.child(button(bcx, &t, &a, On::Page, true, move || {
                        banner_action(pcx, &c, id)
                    }));
                }
                Element::new()
                    .style(LayoutStyle::column().shrink(0.0))
                    .child(super::w::form::sentence(&t, &text, w, t.warn))
                    .child(row.build())
                    .build()
            }
            Loadable::Failed(e) => super::w::form::sentence(
                &t,
                &format!("model availability unavailable: {e}"),
                w,
                t.text_muted,
            ),
            _ => Element::new().style(LayoutStyle::default().h(0)).build(),
        }
    })
}

/// The web banner's sentence (`renderAvailabilityBanner`).
pub fn gaps_sentence(n: usize, routes: &str, pairs: &str) -> String {
    let head = if n == 1 {
        "One route has".to_string()
    } else {
        format!("{n} routes have")
    };
    format!("{head} no model yet ({routes}). Recommended to get started: {pairs}. ")
        .trim_end()
        .to_string()
}

fn banner_action(cx: Scope, ctx: &Ctx, id: &str) {
    match id {
        "download_missing" => download_missing(cx, ctx),
        "download_all" => download_all(cx, ctx),
        "plan" => open_plan(cx, ctx),
        "cancel_all" => cancel_download_all(cx, ctx),
        _ => {}
    }
}

fn plan_region(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
        let t = tt;
        let store = ctx.store;
        let ui = ctx.ui;
        let avail = page_w(gcx).max(20) as usize;
        let wizard = ui.wizard.get();
        let mut rows: Vec<View> = Vec::new();
        let group = store.download_group.get();
        if let Some(g) = &group {
            rows.push(line(vec![span_bold(
                widths::middle_fit(&g.line(), avail as i32),
                group_tone(&t, g),
            )]));
        }
        let plan = store
            .availability
            .with(|a| a.ready().map(|a| a.plan.clone()).unwrap_or_default());
        if !plan.is_empty() {
            let installed = plan.iter().filter(|r| r.status == "installed").count();
            let warned = plan.iter().filter(|r| r.warning.is_some()).count();
            let mut head = format!(
                "recommended for this computer: {installed} of {} installed",
                plan.len()
            );
            if warned > 0 {
                head.push_str(&format!(
                    " · {warned} fit warning{}",
                    if warned == 1 { "" } else { "s" }
                ));
            }
            let missing = plan.iter().filter(|r| r.engine_missing.is_some()).count();
            if missing > 0 {
                head.push_str(&format!(
                    " · {missing} engine{} missing",
                    if missing == 1 { "" } else { "s" }
                ));
            }
            let attention = warned > 0 || missing > 0;
            rows.push(super::w::form::sentence(
                &t,
                &head,
                avail as i32,
                if attention { t.warn } else { t.text },
            ));
            if wizard {
                for r in &plan {
                    let mut spans = vec![
                        span(format!("  {:<14}", r.title()), t.text_muted),
                        span(
                            format!("{:<15}", r.status_label()),
                            status_tone(&t, &r.status),
                        ),
                        span(format!("{} {}", r.provider, r.artifact), t.text),
                    ];
                    if let Some(m) = &r.engine_missing {
                        spans.push(span(format!("  ⚠ {}", m.text()), t.warn));
                    } else if let Some(g) = r.gpu_limit_text() {
                        spans.push(span(format!("  {g}"), t.info));
                    } else if let Some(w) = &r.warning {
                        spans.push(span(format!("  ⚠ {w}"), t.warn));
                    }
                    rows.push(line(spans));
                }
            }
        }
        if !plan.is_empty() || group.is_some() {
            let admin = store.conn.with(ConnPhase::is_admin);
            let running = group.as_ref().map(GroupStatus::running).unwrap_or(false);
            let can_all = can_download_all(&plan, group.as_ref());
            let mut row = Element::new().style(
                LayoutStyle::row()
                    .height(Dimension::Cells(1))
                    .gap(1)
                    .shrink(0.0),
            );
            for a in plan_actions(admin, running, can_all) {
                let c = ctx.clone();
                let id = a.id;
                row = row.child(button(gcx, &t, &a, On::Page, true, move || {
                    banner_action(pcx, &c, id)
                }));
            }
            rows.push(row.build());
        }
        Element::new()
            .style(LayoutStyle::column().shrink(0.0))
            .children(rows)
            .build()
    })
}

/// The table region (rebuilt when the rows, the weights, the role or the
/// width change). Row actions open on the PAGE scope (`pcx`).
fn table_region(pcx: Scope, ctx: &Ctx, t: &TokenSet, sel_key: Signal<Option<String>>) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    let keeper = super::util::FocusKeeper::new();
    dyn_view_scoped(LayoutStyle::column().grow(1.0), move |gcx| {
        let store = ctx.store;
        let data = store.routes.get();
        let ctx = ctx.clone();
        super::util::loadable_view_kept(
            &keeper,
            &tt,
            &store.conn.get(),
            || store.tick.get(),
            &data,
            |_d: &RoutesData| false,
            EMPTY,
            |d| routes_table(gcx, pcx, &ctx, &tt, d, sel_key),
        )
    })
}

/// The DataTable of routes.
fn routes_table(
    gcx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    data: &RoutesData,
    sel_key: Signal<Option<String>>,
) -> View {
    use super::w::Ink;
    let store = ctx.store;
    let admin = store.conn.with(ConnPhase::is_admin);
    let weights = store
        .availability
        .with(|a| a.ready().map(|a| a.by_route.clone()).unwrap_or_default());
    let dl = store.download.get();
    let vp = crate::ui::page_viewport(gcx).get();
    let w = (vp.w - 2).max(20);
    let wide = w >= 110;
    let rows: Vec<WRow> = data
        .rows
        .iter()
        .map(|r| {
            let wt = weights.get(&r.key);
            let downloading = match (&dl, wt) {
                (Some(d), Some(wt)) => {
                    d.running() && d.provider == wt.provider && d.artifact == wt.artifact
                }
                _ => false,
            };
            let lock = if !r.editable() { " ⊘" } else { "" };
            let route = format!("{}{}", r.display_key(), lock);
            let capability = if r.is_task_parent() {
                format!("{} — any {} task (fallback)", r.label, r.modality)
            } else {
                r.label.clone()
            };
            let mut model = or_dash(&r.model);
            if r.is_text_generation() {
                if let Some(re) = r.reasoning.as_deref().filter(|x| !x.is_empty()) {
                    model.push_str(&format!(" · reasoning {re}"));
                }
            }
            let status = status_label(r);
            let status_ink = match status.as_str() {
                "configured" => t.ok,
                "not configured" | "cannot run here" | "engine missing" => t.warn,
                _ => t.text_muted,
            };
            let weights_cell = if downloading {
                let d = dl.clone().unwrap();
                Cell::text(
                    match d.percent {
                        Some(p) => format!("Downloading {p:.0}%"),
                        None => "Downloading…".to_string(),
                    },
                    t.info,
                )
            } else {
                match wt {
                    Some(w) => {
                        let (label, tone, _) = weight_view(w);
                        let ink = match tone {
                            WeightTone::Ok => t.ok,
                            WeightTone::Warn => t.warn,
                            WeightTone::Info => t.info,
                            WeightTone::Muted => t.text_muted,
                        };
                        let tip = weight_tip(w);
                        Cell::Badge {
                            label: label.to_string(),
                            ink,
                            action: None,
                            tip: (!tip.is_empty()).then_some(tip),
                        }
                    }
                    None => Cell::text("-", t.text_faint),
                }
            };
            let acts = row_actions(r, wt, downloading, admin);
            let words = readonly_words(r);
            let (actions, note) = match (words, acts.is_empty()) {
                (Some((w, _)), true) => (Cell::text(w, t.text_muted), None),
                (Some((w, _)), false) => (Cell::Actions(acts), Some((w, t.text_muted))),
                (None, _) => (Cell::Actions(acts), None),
            };
            let cells = if wide {
                vec![
                    Cell::text(route, t.text),
                    Cell::text(capability, t.text),
                    Cell::text(or_dash(&r.provider), t.text),
                    Cell::text(model, t.text),
                    weights_cell,
                    Cell::text(source_label(r), t.text_muted),
                    Cell::text(status, status_ink),
                    actions,
                ]
            } else {
                vec![
                    Cell::Lines(vec![
                        vec![Ink::new(route, t.text)],
                        vec![Ink::new(capability, t.text_muted)],
                    ]),
                    Cell::Lines(vec![
                        vec![Ink::new(model, t.text)],
                        vec![Ink::new(or_dash(&r.provider), t.text_muted)],
                    ]),
                    weights_cell,
                    Cell::text(status, status_ink),
                    actions,
                ]
            };
            WRow::new(r.key.clone(), cells).dim(read_only(r)).note(note)
        })
        .collect();
    let cols = if wide {
        vec![
            Col::new("Route", ColW::Fit { min: 10, max: 30 }),
            Col::new("Capability", ColW::Flex { weight: 1, min: 10 }),
            Col::new("Provider", ColW::Fit { min: 8, max: 18 }),
            Col::new("Model", ColW::Fit { min: 14, max: 52 }),
            Col::new("Weights", ColW::Fit { min: 7, max: 16 }),
            Col::new("Source", ColW::Fit { min: 6, max: 18 }),
            Col::new("Status", ColW::Fit { min: 6, max: 22 }),
            Col::new("Actions", ColW::Fit { min: 7, max: 12 }),
        ]
    } else {
        vec![
            Col::new("Route", ColW::Flex { weight: 1, min: 12 }),
            Col::new("Model", ColW::Flex { weight: 1, min: 12 }),
            Col::new("Weights", ColW::Fit { min: 7, max: 15 }),
            Col::new("Status", ColW::Fit { min: 6, max: 22 }),
            Col::new("Actions", ColW::Fit { min: 7, max: 12 }),
        ]
    };
    let max_rows = (vp.h - 10).max(4);
    let ctx_a = ctx.clone();
    let ctx_e = ctx.clone();
    DataTable::new(cols, rows, sel_key)
        .width(w)
        .max_rows(max_rows)
        .empty(EMPTY)
        .autofocus()
        .on_action(move |key, id| row_action(pcx, &ctx_a, key, id))
        .on_activate(move |key| {
            select_key(&ctx_e, key);
            edit_selected(pcx, &ctx_e);
        })
        .view(gcx, t)
}

/// Select the route `key` (the legacy index paths read it at once).
fn select_key(ctx: &Ctx, key: &str) {
    let keys = ctx.store.routes.with_untracked(|d| {
        d.ready()
            .map(|d| d.rows.iter().map(|r| r.key.clone()).collect::<Vec<_>>())
            .unwrap_or_default()
    });
    if let Some(i) = keys.iter().position(|k| k == key) {
        if ctx.ui.route_sel.get_untracked() != i {
            ctx.ui.route_sel.set(i);
        }
    }
}

/// A row action (a click or its key): the row becomes the selection, the
/// action runs.
fn row_action(cx: Scope, ctx: &Ctx, key: &str, id: &str) {
    select_key(ctx, key);
    match id {
        "edit" | "configure" => edit_selected(cx, ctx),
        "clear" => clear_selected(cx, ctx),
        "download" => download_selected(ctx),
        "copy" => copy_selected(ctx),
        _ => {}
    }
}

/// [⤓ Download missing]: EXACTLY the gaps the banner named, one download
/// each (the web's `downloadRecommended(gaps)`), after a confirm naming them.
fn download_missing(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "downloading model weights") {
        return;
    }
    let gaps = ctx
        .store
        .availability
        .with_untracked(|a| a.ready().map(|a| a.missing.clone()).unwrap_or_default());
    if gaps.is_empty() {
        ctx.store.notice.set(Some(
            "no route is missing a model — nothing to download".into(),
        ));
        return;
    }
    let list = gaps
        .iter()
        .map(|(_, p, a)| format!("{p} {a}"))
        .collect::<Vec<_>>()
        .join(", ");
    let c = ctx.clone();
    super::w::Confirm::plain(
        format!("Download {list} on the gateway host? Each one runs its provider's own tool."),
        "Download",
        "Not now",
    )
    .open(cx, ctx.ui, move || {
        for (_, provider, artifact) in gaps {
            c.send(Cmd::DownloadModel { provider, artifact });
        }
    });
}

/// Copy the selected row's install command (no download verb here).
fn copy_selected(ctx: &Ctx) {
    let Some(row) = selected_route(ctx) else {
        return;
    };
    let cmd = ctx.store.availability.with_untracked(|a| {
        a.ready()
            .and_then(|a| a.by_route.get(&row.key))
            .map(|w| w.instruction.trim().to_string())
    });
    match cmd.filter(|c| !c.is_empty()) {
        Some(c) => {
            copy_to_clipboard(c.clone());
            ctx.store.notice.set(Some(format!("Copied: {c}")));
        }
        None => ctx
            .store
            .notice
            .set(Some(format!("{}: no command to copy", row.key))),
    }
}

/// The page's hint pairs (R15: the selected row's actions, then the page's).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let store = ctx.store;
    let admin = store.conn.with(ConnPhase::is_admin);
    let _ = ctx.ui.route_sel.get();
    let mut out = vec![("↑↓", "rows"), ("Enter", "Configure"), ("Tab", "actions")];
    if let Some(r) = store.routes.with(|d| {
        d.ready()
            .and_then(|d| d.rows.get(ctx.ui.route_sel.get()).cloned())
    }) {
        let wt = store
            .availability
            .with(|a| a.ready().and_then(|a| a.by_route.get(&r.key).cloned()));
        // Every principal sees the row's verbs; the admin-only ones (w, and
        // a/m/D below) are folded into "… admin only" for a non-admin by
        // the footer (`ADMIN_KEYS`).
        for a in row_actions(&r, wt.as_ref(), false, true) {
            match a.id {
                "edit" => out.push(("e", "Edit")),
                "configure" => out.push(("e", "Configure")),
                "clear" => out.push(("x", "Clear")),
                "download" => out.push(("w", "Download")),
                "copy" => out.push(("c", "Copy")),
                _ => {}
            }
        }
    }
    let _ = admin;
    out.push(("a", "Apply recommended"));
    out.push(("m", "Download missing"));
    out.push(("D", "Download all"));
    out.push(("p", "Recommended for this computer"));
    out.push(("r", "Refresh"));
    out
}

fn selected_route(ctx: &Ctx) -> Option<RouteRow> {
    let idx = ctx.ui.route_sel.get_untracked();
    ctx.store
        .routes
        .with_untracked(|d| d.ready().and_then(|d| d.rows.get(idx).cloned()))
}

fn edit_selected(cx: Scope, ctx: &Ctx) {
    let Some(row) = selected_route(ctx) else {
        // F2: refusals name their reason — silent keys read as dead.
        ctx.store
            .notice
            .set(Some("no route selected — nothing to edit".into()));
        return;
    };
    if !ctx.store.conn.with_untracked(ConnPhase::is_connected) {
        ctx.store.notice.set(Some(
            "not connected — probe on the Connection screen first".into(),
        ));
        return;
    }
    if !row.editable() {
        let reason = if let Some(d) = &row.derived_from {
            format!("{} derives from {} — edit that route instead", row.key, d)
        } else if row.covered_by.is_some() {
            format!("{} is covered and not overrideable", row.key)
        } else {
            format!("{} is read-only", row.key)
        };
        ctx.store.notice.set(Some(reason));
        return;
    }
    open_route_editor(cx, ctx, row);
}

fn clear_selected(cx: Scope, ctx: &Ctx) {
    let Some(row) = selected_route(ctx) else {
        ctx.store
            .notice
            .set(Some("no route selected — nothing to clear".into()));
        return;
    };
    // Clearing only makes sense for an explicitly configured row.
    if !(row.configured && row.covered_by.is_none() && row.editable()) {
        ctx.store.notice.set(Some(format!(
            "{} has no explicit override to clear",
            row.key
        )));
        return;
    }
    confirm_clear(cx, ctx, row);
}

/// `a` — make the execution host's routes match the framework
/// recommendation.
///
/// Three answers, because the honest action has three: fill only the
/// empty routes (safe, the default), replace the operator's choices too
/// (the `force` spelling, offered as its own danger-tinted option so it
/// can never be the accidental one), or nothing. Which routes the
/// gateway kept is named in the journal line, from the server's own
/// report — this console never re-derives that decision.
fn apply_recommended(cx: Scope, ctx: &Ctx) {
    // Admin-only server-side (apply-recommended rewrites the HOST-WIDE
    // store); the web hides its button for a non-admin.
    if !super::util::admin_gate(&ctx.store, "applying the recommended routes") {
        return;
    }
    let ctx_keep = ctx.clone();
    super::w::Confirm::plain(APPLY_QUESTION, "Apply recommended", "Cancel").open(
        cx,
        ctx.ui,
        move || ctx_keep.send(Cmd::ApplyRecommendedRoutes { force: false }),
    );
}

/// The question Apply recommended asks (the first pass never forces: the
/// routes you configured are kept).
pub const APPLY_QUESTION: &str = "Apply the framework's recommended routes (text, voice, transcription, images, video — what this computer can run) on the execution host? Routes you configured differently are kept.";

fn status_tone(t: &TokenSet, status: &str) -> Rgba {
    match status {
        "installed" => t.ok,
        "absent" => t.warn,
        _ => t.text_muted,
    }
}

fn group_tone(t: &TokenSet, g: &GroupStatus) -> Rgba {
    if g.running() {
        t.info
    } else if g.status == "completed" {
        t.ok
    } else if g.status == "cancelled" {
        t.text_muted
    } else {
        t.error
    }
}

/// `p` — the recommended plan in full: every card of the web guide's
/// model step (status, provider, artifact, memory tier, the fit warning
/// verbatim, the evidence and the CLI), plus the Download-all job.
fn open_plan(cx: Scope, ctx: &Ctx) {
    let plan = ctx
        .store
        .availability
        .with_untracked(|a| a.ready().map(|a| a.plan.clone()));
    let Some(plan) = plan else {
        ctx.store.notice.set(Some(
            "the recommended plan is not loaded yet (r reloads the weights)".into(),
        ));
        return;
    };
    let store = ctx.store;
    let size = super::preview_size(cx);
    super::w::FormModal::new("Recommended for this computer")
        .size(size.w.max(76), size.h.max(20))
        .open(ctx, cx, move |mcx, close, _guard, inner_w| {
        let t = use_theme(mcx).get().tokens;
        let width = inner_w.max(20) as usize;
        let mut rows: Vec<View> = Vec::new();
        let current = store.routes.with_untracked(|r| {
            r.ready().and_then(|d| {
                d.rows
                    .iter()
                    .find(|row| row.key == "output.text" && row.model.is_some())
                    .or_else(|| {
                        d.rows
                            .iter()
                            .find(|row| row.key == "input.text" && row.model.is_some())
                    })
                    .map(|row| row.pair_text())
            })
        });
        rows.push(line(vec![span(
            match current {
                Some(c) => format!("Text model now: {c}"),
                None => "No text model is set yet.".to_string(),
            },
            t.text_muted,
        )]));
        rows.push(line(vec![span(String::new(), t.text)]));
        if plan.is_empty() {
            rows.push(line(vec![span(
                "This gateway reported no recommended downloads.",
                t.text_muted,
            )]));
        }
        for r in &plan {
            rows.push(line(vec![
                span_bold(format!("{}  ", r.title()), t.text),
                span(r.status_label().to_string(), status_tone(&t, &r.status)),
            ]));
            rows.push(line(vec![span(
                format!("  {} {}  (route {})", r.provider, r.artifact, r.route),
                t.text,
            )]));
            if let Some(tier) = &r.tier {
                rows.push(line(vec![span(
                    format!("  Chosen by memory: {tier}"),
                    t.text_faint,
                )]));
            }
            if let Some(g) = r.gpu_limit_text() {
                for l in super::util::wrap_text(&g, width.saturating_sub(4)) {
                    rows.push(line(vec![span(format!("  {l}"), t.info)]));
                }
            }
            if let Some(m) = &r.engine_missing {
                for l in super::util::wrap_text(&m.text(), width.saturating_sub(4)) {
                    rows.push(line(vec![span(format!("  {l}"), t.warn)]));
                }
            }
            if let Some(w) = &r.warning {
                for (i, l) in super::util::wrap_text(w, width.saturating_sub(4))
                    .into_iter()
                    .enumerate()
                {
                    rows.push(line(vec![span(
                        format!("  {}{l}", if i == 0 { "⚠ " } else { "  " }),
                        t.warn,
                    )]));
                }
            }
            if let Some(e) = &r.evidence {
                rows.push(line(vec![span(format!("  evidence: {e}"), t.text_faint)]));
            }
            if let Some(i) = &r.instruction {
                rows.push(line(vec![span(format!("  CLI: {i}"), t.text_faint)]));
            }
            rows.push(line(vec![span(String::new(), t.text)]));
        }
        // Recommended routes this host cannot run (AbstractCore's
        // host-aware defaults): left unset, never written, with the reason.
        let unavailable: Vec<(String, crate::store::RecommendationUnavailable)> =
            store.routes.with_untracked(|r| {
                r.ready()
                    .map(|d| {
                        d.rows
                            .iter()
                            .filter_map(|row| {
                                row.recommendation_unavailable
                                    .clone()
                                    .map(|u| (row.key.clone(), u))
                            })
                            .collect()
                    })
                    .unwrap_or_default()
            });
        if !unavailable.is_empty() {
            rows.push(line(vec![span_bold(
                "Not available on this computer (left unset)",
                t.warn,
            )]));
            for (key, u) in &unavailable {
                rows.push(line(vec![span(
                    format!("  {key}: recommended {} {}", u.provider, u.model),
                    t.text,
                )]));
                for l in super::util::wrap_text(&u.reason, width.saturating_sub(4)) {
                    rows.push(line(vec![span(format!("    {l}"), t.text_muted)]));
                }
            }
            rows.push(line(vec![span(String::new(), t.text)]));
        }
        // Configured routes this host cannot run (`route_unavailable`): a
        // plan that listed only the gaps would call these routes fine.
        let broken: Vec<(String, crate::store::RecommendationUnavailable)> =
            store.routes.with_untracked(|r| {
                r.ready()
                    .map(|d| {
                        d.rows
                            .iter()
                            // A derived row (output.text ← input.text)
                            // carries its source's flag: listed once.
                            .filter(|row| row.derived_from.is_none())
                            .filter_map(|row| {
                                row.route_unavailable.clone().map(|u| (row.key.clone(), u))
                            })
                            .collect()
                    })
                    .unwrap_or_default()
            });
        if !broken.is_empty() {
            rows.push(line(vec![span_bold(
                "Configured but cannot run on this computer",
                t.warn,
            )]));
            let admin = store.conn.with_untracked(|c| c.is_admin());
            for (key, u) in &broken {
                rows.push(line(vec![span(
                    format!("  {key}: {} {}", u.provider, u.model),
                    t.text,
                )]));
                for l in super::util::wrap_text(&u.reason, width.saturating_sub(4)) {
                    rows.push(line(vec![span(format!("    {l}"), t.text_muted)]));
                }
                rows.push(line(vec![span(
                    format!("    fix: {}", broken_route_fix(key, admin)),
                    t.text_faint,
                )]));
            }
            rows.push(line(vec![span(String::new(), t.text)]));
        }
        if let Some(g) = store.download_group.get_untracked() {
            rows.push(line(vec![span_bold(g.line(), group_tone(&t, &g))]));
            for (name, state) in &g.files {
                rows.push(line(vec![span(format!("  {name}: {state}"), t.text_muted)]));
            }
        }
        rows.push(line(vec![span(
            if store.conn.with_untracked(|c| c.is_admin()) {
                "On Multimodal: Apply recommended (yours are kept) · Download all · Cancel downloads"
            } else {
                "Applying the recommended routes and downloading models are admin-only"
            },
            t.text_faint,
        )]));
        let c = close.clone();
        Element::new()
            .style(LayoutStyle::column().grow(1.0))
            .child(
                Scroll::new(
                    Element::new()
                        .style(LayoutStyle::column())
                        .children(rows)
                        .build(),
                )
                .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                .scrollbar_auto_hide(true)
                .view(mcx),
            )
            .child(super::w::form::button_row(vec![button(
                mcx,
                &t,
                &Action::label("close", "Close"),
                On::Raised,
                true,
                move || c(),
            )]))
            .build()
    });
}

/// `D` — "Download all": the recommended set in ONE parent job, exactly
/// the web guide's `POST /models/download {"recommended": true}`. Offered
/// under the web's rule (some recommended model absent, no group
/// running), always after a confirm that names what will be fetched.
fn download_all(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "Download all") {
        return;
    }
    let plan = ctx
        .store
        .availability
        .with_untracked(|a| a.ready().map(|a| a.plan.clone()));
    let Some(plan) = plan else {
        ctx.store.notice.set(Some(
            "the recommended plan is not loaded yet (r reloads the weights)".into(),
        ));
        return;
    };
    let group = ctx.store.download_group.get_untracked();
    if group.as_ref().map(GroupStatus::running).unwrap_or(false) {
        ctx.store.notice.set(Some(
            "Download all is already running — C cancels it, p shows its progress".into(),
        ));
        return;
    }
    if !can_download_all(&plan, group.as_ref()) {
        ctx.store.notice.set(Some(
            "nothing to download — no recommended model is reported absent on this host".into(),
        ));
        return;
    }
    let list = plan
        .iter()
        .map(|r| format!("{} {} ({})", r.provider, r.artifact, r.status_label()))
        .collect::<Vec<_>>()
        .join(", ");
    let ctx2 = ctx.clone();
    super::w::Confirm::plain(
        format!(
            "Download the recommended set on the gateway host? {list}. Models already there finish at once; the rest run the providers' own tools and may fetch many gigabytes."
        ),
        "Download all",
        "Not now",
    )
    .open(cx, ctx.ui, move || ctx2.send(Cmd::DownloadRecommended));
}

/// `C` — cancel the running Download all (admin; every child stops).
fn cancel_download_all(cx: Scope, ctx: &Ctx) {
    if !super::util::admin_gate(&ctx.store, "cancelling a download") {
        return;
    }
    let Some(g) = ctx
        .store
        .download_group
        .get_untracked()
        .filter(GroupStatus::running)
    else {
        ctx.store.notice.set(Some(
            "no Download all is running — nothing to cancel".into(),
        ));
        return;
    };
    let ctx2 = ctx.clone();
    let job = g.job.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!("Cancel Download all ({job})? Every model still downloading stops."),
        "Cancel downloads",
        "Keep downloading",
        move || ctx2.send(Cmd::CancelDownloadGroup { job: job.clone() }),
    );
}

/// `d` — download the selected route's model weights on the execution
/// host.
///
/// NEVER AUTOMATIC, ALWAYS CONFIRMED, and word-for-word the refusals the
/// AbstractCore console gives for the same states: an installed model, a
/// relay provider with nothing to fetch, and — the important one — an
/// `unknown` answer, where guessing would spend the host's disk on a
/// model that may already be there.
fn download_selected(ctx: &Ctx) {
    // `POST /models/download` spends the shared host's disk: admin-only
    // on the gateway (security/authorization.py, resource "models").
    if !super::util::admin_gate(&ctx.store, "downloading model weights") {
        return;
    }
    let Some(row) = selected_route(ctx) else {
        ctx.store
            .notice
            .set(Some("no route selected — nothing to download".into()));
        return;
    };
    let weights = ctx
        .store
        .availability
        .with_untracked(|d| d.ready().and_then(|a| a.by_route.get(&row.key).cloned()));
    let Some(weights) = weights else {
        ctx.store.notice.set(Some(format!(
            "{}: no weight information yet (r reloads) — nothing to download",
            row.key
        )));
        return;
    };
    match weights.status.as_str() {
        "installed" => {
            // The web console's sentence under the pill (core `summary`),
            // then the probe's own detail when it has one.
            let said = [weights.summary.as_str(), weights.detail.as_str()]
                .iter()
                .filter(|t| !t.is_empty())
                .cloned()
                .collect::<Vec<_>>()
                .join(" ");
            ctx.store.notice.set(Some(format!(
                "{} is already installed{}",
                weights.artifact,
                if said.is_empty() {
                    String::new()
                } else {
                    format!(" ({said})")
                }
            )));
            return;
        }
        "not_applicable" => {
            ctx.store.notice.set(Some(format!(
                "{} serves models remotely — there is nothing to download",
                weights.provider
            )));
            return;
        }
        "unknown" => {
            let hint = if weights.instruction.is_empty() {
                "the provider's tool could not be consulted".to_string()
            } else {
                weights.instruction.clone()
            };
            ctx.store.notice.set(Some(if weights.summary.is_empty() {
                format!("{}: availability is unknown — {hint}", row.key)
            } else {
                format!(
                    "{}: availability is unknown — {} {hint}",
                    row.key, weights.summary
                )
            }));
            return;
        }
        _ => {}
    }
    if !weights.downloadable {
        ctx.store
            .notice
            .set(Some(if weights.instruction.is_empty() {
                format!(
                    "{} has no download tool on the execution host",
                    weights.provider
                )
            } else {
                weights.instruction.clone()
            }));
        return;
    }

    // The confirm names the target AND its size: the worker reads the
    // catalog first, then `store.download_offer` opens the confirm.
    ctx.send(Cmd::PrepareDownload {
        provider: weights.provider.clone(),
        artifact: weights.artifact.clone(),
    });
}

/// ONE confirm policy for clearing a route — shared by the table's `x`
/// and the editor's Clear button (the editor closes itself first, then
/// prompts on the screen scope).
fn confirm_clear(cx: Scope, ctx: &Ctx, row: RouteRow) {
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Clear the override on {} ({} / {})? The engine default takes over.",
            row.key,
            row.provider.clone().unwrap_or_default(),
            row.model.clone().unwrap_or_default()
        ),
        "Clear",
        "Cancel",
        move || {
            ctx2.send(Cmd::ClearRoute {
                kind: row.kind,
                modality: row.modality,
                task: row.task,
                key: row.key,
            })
        },
    );
}

/// "Applies now" — derived ONLY from the server row, never local picks.
fn applies_now(t: &TokenSet, row: &RouteRow) -> View {
    let mut spans = vec![span_bold("Applies now: ".to_string(), t.text)];
    if row.configured {
        spans.push(span(row.pair_text(), t.ok));
        if let Some(r) = &row.reasoning {
            spans.push(span(format!("  · reasoning {r}"), t.ok));
        }
        if let Some(options) = &row.options {
            if let Some(value) = options.get("speculation") {
                let label = match speculation_index(options) {
                    1 => "off".to_string(),
                    n @ 2..=5 => format!("depth {n}"),
                    _ => value.to_string(),
                };
                spans.push(span(format!("  · MTP default {label}"), t.text_muted));
            }
        }
        if let Some(by) = &row.covered_by {
            spans.push(span(format!("  (covered by {by})"), t.info));
        } else {
            spans.push(span(format!("  (source: {})", row.source), t.text_muted));
        }
    } else {
        spans.push(span(
            "nothing configured — engine decides".to_string(),
            t.text_muted,
        ));
        if let Some(hint) = &row.package_hint {
            spans.push(span(format!("  (needs {hint})"), t.text_faint));
        }
    }
    line(spans)
}

const CUSTOM: &str = "custom (type a name)…";

/// The voice-test run id — the SAME session-scoped id shape the web
/// console mints, so both UIs share one test-run plane on the gateway.
fn voice_test_run_id(store: &crate::store::Store) -> String {
    let (tenant, user) = store.conn.with_untracked(|c| match c {
        ConnPhase::Connected(id) => (id.tenant_id.clone(), id.user_id.clone()),
        _ => (String::new(), String::new()),
    });
    voice_test_run_id_for(&tenant, &user)
}

/// The web's `voiceTestRunId` for a principal (parts folded like the
/// sandbox's: one alphabet with the server's run-id pattern).
pub fn voice_test_run_id_for(tenant: &str, user: &str) -> String {
    use super::sandbox::session_memory_id_part as part;
    format!(
        "session_memory_gateway_console_voicetest_{}_{}",
        part(tenant, "default"),
        part(user, "user")
    )
}

/// The (provider, model) the form currently resolves to — EXACTLY the
/// Save handler's resolution (placeholder/blank picks → None). Tracked
/// reads: effects using this re-fire on any pick change.
#[allow(clippy::too_many_arguments)]
fn picked_pair(
    store: &crate::store::Store,
    prov_options: &[String],
    custom_row: usize,
    prov_ix: Signal<usize>,
    prov_custom: Signal<String>,
    model_ix: Signal<usize>,
    model_custom: Signal<String>,
) -> Option<(String, String)> {
    let ix = prov_ix.get();
    if ix == 0 {
        return None;
    }
    let provider = if ix == custom_row {
        let p = prov_custom.get().trim().to_string();
        if p.is_empty() {
            return None;
        }
        p
    } else {
        prov_options[ix - 1].clone()
    };
    let model = if ix != custom_row {
        let has_list = store.models.with(|m| {
            m.get(&prov_options[ix - 1])
                .and_then(|l| l.ready())
                .map(|ms| !ms.is_empty())
                .unwrap_or(false)
        });
        if has_list {
            let mix = model_ix.get();
            if mix == 0 || mix == usize::MAX {
                return None;
            }
            store.models.with(|m| {
                m.get(&prov_options[ix - 1])
                    .and_then(|l| l.ready())
                    .and_then(|ms| ms.get(mix - 1).cloned())
            })?
        } else {
            let mc = model_custom.get().trim().to_string();
            if mc.is_empty() {
                return None;
            }
            mc
        }
    } else {
        let mc = model_custom.get().trim().to_string();
        if mc.is_empty() {
            return None;
        }
        mc
    };
    Some((provider, model))
}

/// The voice picker WRITES INTO the options JSON — the single source of
/// truth the save path reads, so what the field shows is exactly what
/// will be stored. ix 0 = provider default (removes the key).
/// The options text the editor showed at open (its prefill) — what a save
/// compares against to know whether the operator edited the options.
fn shown_options(row: &RouteRow) -> String {
    row.options
        .as_ref()
        .map(|o| o.to_string())
        .unwrap_or_default()
}

/// The route save body, the web's exact rule (console.py saveDefault):
/// THE SAVE SENDS WHAT THE EDITOR OWNS AND THE OPERATOR CHANGED.
/// `{provider, model}` always; `reasoning` on the text route only, always
/// explicit ("" clears it); `base_url` and `options` ONLY when they differ
/// from what the editor showed — both are prefilled from a grid read that
/// may be minutes old, so naming them unconditionally would roll back a
/// change made meanwhile (e.g. `abstractcore config`). A field the operator
/// emptied differs from what was shown, so it IS sent ("" / `{}`), which is
/// how an override gets cleared. Options text is validated whenever there
/// is some, edited or not. `base_url` and `options` are (now, shown).
pub fn route_save_body(
    provider: &str,
    model: &str,
    reasoning: Option<&str>,
    base_url: (&str, &str),
    options: (&str, &str),
) -> Result<Value, String> {
    let mut body = json!({ "provider": provider, "model": model });
    if let Some(r) = reasoning {
        body["reasoning"] = Value::String(r.to_string());
    }
    let (url_now, url_shown) = (base_url.0.trim(), base_url.1.trim());
    if url_now != url_shown {
        body["base_url"] = Value::String(url_now.to_string());
    }
    let (opts_now, opts_shown) = (options.0.trim(), options.1.trim());
    let parsed = if opts_now.is_empty() {
        json!({})
    } else {
        match serde_json::from_str::<Value>(opts_now) {
            Ok(v) if v.is_object() => v,
            Ok(_) => return Err("options must be a JSON object".into()),
            Err(e) => return Err(format!("options JSON does not parse: {e}")),
        }
    };
    if opts_now != opts_shown {
        body["options"] = parsed;
    }
    Ok(body)
}

fn merge_voice_into_options(
    options_json: Signal<String>,
    form_error: Signal<Option<String>>,
    voices: &[String],
    ix: usize,
) {
    let text = options_json.get_untracked();
    let trimmed = text.trim();
    let mut obj = if trimmed.is_empty() {
        serde_json::Map::new()
    } else {
        match serde_json::from_str::<Value>(trimmed) {
            Ok(Value::Object(o)) => o,
            // Never clobber typed work the picker cannot merge into.
            _ => {
                form_error.set(Some(
                    "options JSON does not parse — fix it before picking a voice".into(),
                ));
                return;
            }
        }
    };
    if ix == 0 {
        obj.remove("voice");
    } else if let Some(v) = voices.get(ix - 1) {
        obj.insert("voice".into(), Value::String(v.clone()));
    }
    if obj.is_empty() {
        options_json.set(String::new());
    } else {
        options_json.set(Value::Object(obj).to_string());
    }
}

/// The "voice" the options JSON currently carries (save-path truth).
fn options_voice(options_json: &str) -> Option<String> {
    serde_json::from_str::<Value>(options_json.trim())
        .ok()
        .and_then(|v| {
            v.get("voice")
                .and_then(Value::as_str)
                .map(str::to_string)
                .filter(|s| !s.is_empty())
        })
}

/// The web's route-editor split (console.py: the options box hides
/// `speculation` on a text-generation route; the MTP selector owns it):
/// (what the options box shows, the `{"speculation": …}` the selector
/// edits — "" when unset). Other routes show every option.
pub fn split_route_options(options: Option<&Value>, is_text: bool) -> (String, String) {
    let Some(Value::Object(map)) = options else {
        return (
            options.map(|o| o.to_string()).unwrap_or_default(),
            String::new(),
        );
    };
    if !is_text {
        return (Value::Object(map.clone()).to_string(), String::new());
    }
    let mut rest = map.clone();
    let spec = rest.remove("speculation");
    let shown = if rest.is_empty() {
        String::new()
    } else {
        Value::Object(rest).to_string()
    };
    let spec = spec
        .map(|v| json!({ "speculation": v }).to_string())
        .unwrap_or_default();
    (shown, spec)
}

/// The options a save or test sends: the box, plus the selector's
/// `speculation` on a text route. `speculation` typed into the box is
/// refused with the web's words ("Use the MTP selector …"). A box that
/// does not parse is passed through unchanged (the save's own
/// validation names that error).
pub fn compose_route_options(shown: &str, spec: &str, is_text: bool) -> Result<String, String> {
    if !is_text {
        return Ok(shown.to_string());
    }
    let mut map = if shown.trim().is_empty() {
        serde_json::Map::new()
    } else {
        match serde_json::from_str::<Value>(shown) {
            Ok(Value::Object(m)) => m,
            _ => return Ok(shown.to_string()),
        }
    };
    if map.contains_key("speculation") {
        return Err("Use the MTP selector for speculation; the options box edits the remaining provider settings.".into());
    }
    if let Ok(Value::Object(s)) = serde_json::from_str::<Value>(spec) {
        if let Some(v) = s.get("speculation") {
            map.insert("speculation".into(), v.clone());
        }
    }
    Ok(if map.is_empty() {
        String::new()
    } else {
        Value::Object(map).to_string()
    })
}

/// A selector is an editor for options.speculation, never a second setting.
fn speculation_index(options: &Value) -> usize {
    match options.get("speculation") {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(false)) => 1,
        Some(value) if value.get("mode").and_then(Value::as_str) == Some("off") => 1,
        Some(value) => value
            .get("num_draft_tokens")
            .and_then(Value::as_u64)
            .filter(|n| (2..=5).contains(n))
            .map(|n| n as usize)
            .unwrap_or(6),
    }
}

fn speculation_options(text: &str, ix: usize) -> Result<String, String> {
    let mut options = if text.trim().is_empty() {
        serde_json::Map::new()
    } else {
        match serde_json::from_str::<Value>(text) {
            Ok(Value::Object(value)) => value,
            _ => return Err("options JSON does not parse — fix it before picking MTP".into()),
        }
    };
    match ix {
        0 => {
            options.remove("speculation");
        }
        1 => {
            options.insert("speculation".into(), Value::Bool(false));
        }
        2..=5 => {
            let mut control = options
                .get("speculation")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            control.insert("mode".into(), json!("native_mtp"));
            control.insert("num_draft_tokens".into(), json!(ix));
            control.insert("require_acceleration".into(), json!(false));
            options.insert("speculation".into(), Value::Object(control));
        }
        _ => {} // A custom stored value is preserved until the operator edits it.
    }
    Ok(if options.is_empty() {
        String::new()
    } else {
        Value::Object(options).to_string()
    })
}

/// The editor's title and purpose line (the web modal's words).
pub const DIALOG_TITLE: &str = "Configure capability default";
pub const DIALOG_LEAD: &str = "Select a provider and one of its discovered models.";
/// The mode choice (the fabricated-selection law: default vs override).
pub const MODE_LABELS: [&str; 2] = [
    "use default (engine decides)",
    "override: pick provider + model",
];
/// The editor buttons' tooltips (the web's `title`).
pub const CLEAR_TIP: &str = "Remove this override — the route falls back to what it inherits";
pub const TEST_TIP: &str = "Test this selection with a real generation through the production lane — for voice routes, hear the selected voice before saving";
pub const SAVE_TIP: &str = "Persist this provider/model as the capability default";

/// The editor's buttons in the web's order (Cancel · Clear · Test · Save),
/// each refused with its reason while it cannot act.
pub fn editor_actions(
    save: Result<(), String>,
    test: Result<(), String>,
    clear: Result<(), String>,
) -> Vec<Action> {
    let mut c = Action::label("clear", "Clear").tooltip(CLEAR_TIP).danger();
    c.enabled = clear;
    let mut te = Action::label("test", "Test").tooltip(TEST_TIP);
    te.enabled = test;
    let mut sv = Action::label("save", "Save").tooltip(SAVE_TIP);
    sv.enabled = save;
    vec![Action::label("cancel", "Cancel"), c, te, sv]
}

/// A labelled row of the editor (the label column fits "Options (JSON,
/// optional)", the web's longest label).
fn efield(t: &TokenSet, label: &str, child: View) -> View {
    super::util::field_w(t, label, 25, child)
}

pub fn open_route_editor(cx: Scope, ctx: &Ctx, row: RouteRow) {
    let store = ctx.store;
    let providers = super::providers::provider_names(&store);
    let ctx2 = ctx.clone();
    // The SCREEN scope: the editor's Clear button prompts on it after
    // closing the editor (the modal scope dies with the editor).
    let screen_cx = cx;

    // Single-slot state owned by this editor: stale results from a
    // previous editor must never render here.
    store.voices.set(Loadable::NotAsked);
    store.route_test.set(Loadable::NotAsked);

    super::w::FormModal::new(DIALOG_TITLE)
        .lead(DIALOG_LEAD)
        .size(86, 36)
        .open(ctx, cx, move |mcx, close, guard, _inner_w| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        let row2 = row.clone();
        // The voice picker + voice test lane exist only for the voice
        // output route (exactly the web's isVoiceOutputDefault check).
        let is_voice = row.key == "output.voice";
        // The reasoning select exists only on the text-generation route
        // (the web's isTextGenerationDefault check) — the effort is a
        // property of text generation, and Core stores it there.
        let is_text = row.is_text_generation();

        // ---- mode: 0 = engine default, 1 = explicit override ----------
        let explicit = row.configured && row.covered_by.is_none();
        let mode = mcx.signal(if explicit { 1usize } else { 0usize });

        // ---- provider picker: [placeholder] + discovered + custom -----
        let mut prov_options: Vec<String> = providers.clone();
        // The row's current provider may be a non-LLM engine provider
        // (supertonic, mlx-gen…) — make it pickable.
        if let Some(p) = &row.provider {
            if !p.is_empty() && !prov_options.iter().any(|x| x == p) {
                prov_options.insert(0, p.clone());
            }
        }
        let prov_ix = mcx.signal(if explicit {
            row.provider
                .as_ref()
                .and_then(|p| prov_options.iter().position(|x| x == p))
                .map(|i| i + 1)
                .unwrap_or(0)
        } else {
            0usize
        });
        let prov_custom = mcx.signal(String::new());
        let custom_row = prov_options.len() + 1; // select index of CUSTOM

        // ---- model picker (per provider; resets on provider change) ---
        let model_ix = mcx.signal(if explicit { usize::MAX } else { 0usize });
        let model_custom = mcx.signal(row.model.clone().unwrap_or_default());
        // ---- voice picker (output.voice only; sentinel = resolve the
        // saved options.voice against the list once it arrives) --------
        let voice_ix = mcx.signal(usize::MAX);
        let base_url = mcx.signal(row.base_url.clone().unwrap_or_default());
        // Reasoning is a fixed vocabulary, not free text: the same five
        // choices the web console's select offers, index 0 = "not set".
        let reasoning_init_ix = crate::store::reasoning_index(row.reasoning.as_deref());
        let reasoning_ix = mcx.signal(reasoning_init_ix);
        // The options box shows everything but a text route's
        // `speculation`, which the MTP selector owns (web parity).
        let (shown_init, spec_init) = split_route_options(row.options.as_ref(), is_text);
        let options_json = mcx.signal(shown_init);
        let spec_json = mcx.signal(spec_init.clone());
        let speculation_ix = mcx.signal(speculation_index(
            &serde_json::from_str::<Value>(&spec_init).unwrap_or(Value::Null),
        ));
        let form_error = mcx.signal(Option::<String>::None);
        let in_flight = mcx.signal(false);
        let esc_armed = mcx.signal(false);
        let form_id = crate::worker::next_form_id();

        // Dirty-Esc guard + disarm: the shared contract (F4). Mode and
        // provider flips and typed text arm the warning; a
        // picked-but-unsaved model alone deliberately does not (two
        // keystrokes to redo, vs typed JSON/URLs which are real work).
        {
            let initial = (
                mode.get_untracked(),
                prov_ix.get_untracked(),
                prov_custom.get_untracked(),
                model_custom.get_untracked(),
                base_url.get_untracked(),
                options_json.get_untracked(),
                spec_json.get_untracked(),
            );
            super::install_dirty_guard_with(
                mcx,
                &guard,
                move || {
                    mode.get_untracked() != initial.0
                        || prov_ix.get_untracked() != initial.1
                        || prov_custom.get_untracked() != initial.2
                        || model_custom.get_untracked() != initial.3
                        || base_url.get_untracked() != initial.4
                        || options_json.get_untracked() != initial.5
                        || spec_json.get_untracked() != initial.6
                        || reasoning_ix.get_untracked() != reasoning_init_ix
                },
                move || {
                    let _ = (
                        mode.get(),
                        prov_ix.get(),
                        prov_custom.get(),
                        model_custom.get(),
                        base_url.get(),
                        options_json.get(),
                        spec_json.get(),
                        reasoning_ix.get(),
                    );
                },
                esc_armed,
                form_error,
            );
        }

        // Load models when a discovered provider is picked.
        {
            let prov_options = prov_options.clone();
            let ctx3 = ctx2.clone();
            mcx.effect(move || {
                let ix = prov_ix.get();
                if ix == 0 || ix >= custom_row {
                    return;
                }
                let name = prov_options[ix - 1].clone();
                // Absent OR previously-failed entries load (a Failed
                // entry left alone would make a transient blip permanent
                // for the session — "discovery failed" ≠ "no models").
                // UNTRACKED cache read: tracking the map here would
                // re-fire this effect when the failure lands = an
                // unbounded retry loop. Only the provider pick (and the
                // editor opening) trigger a retry.
                let needs = store.models.with_untracked(|m| {
                    !m.contains_key(&name) || matches!(m.get(&name), Some(Loadable::Failed(_)))
                });
                if needs {
                    store
                        .models
                        .update(|m| drop(m.insert(name.clone(), Loadable::Loading)));
                    ctx3.send(Cmd::LoadModels { provider: name });
                }
            });
        }

        // Resolve the saved-model preselect sentinel (usize::MAX) once the
        // provider's model list arrives. The law: a saved model is only
        // selectable under its own saved provider — any other provider
        // resolves to the placeholder. Runs as an EFFECT: render closures
        // never write signals.
        {
            let prov_options = prov_options.clone();
            let saved_provider = row.provider.clone();
            let saved_model = row.model.clone().unwrap_or_default();
            mcx.effect(move || {
                if model_ix.get() != usize::MAX {
                    return;
                }
                let ix = prov_ix.get();
                if ix == 0 || ix >= custom_row {
                    model_ix.set(0);
                    return;
                }
                let name = prov_options[ix - 1].clone();
                let ready = store
                    .models
                    .with(|m| m.get(&name).and_then(|l| l.ready()).cloned());
                match ready {
                    Some(models) if !models.is_empty() => {
                        let pos = if Some(&name) == saved_provider.as_ref() {
                            models.iter().position(|m| *m == saved_model).map(|i| i + 1)
                        } else {
                            None
                        };
                        model_ix.set(pos.unwrap_or(0));
                    }
                    Some(_) => model_ix.set(0), // empty list → free-text lane
                    None => {}                  // still loading — stay armed
                }
            });
        }

        // Voice routes: load the voice catalog whenever the picked
        // (provider, model) pair changes. The last-requested pair is
        // tracked UI-side; the worker serializes commands, so the last
        // request's result is the last posted — no torn responses.
        let voices_req = mcx.signal(Option::<(String, String)>::None);
        if is_voice {
            let prov_options_v = prov_options.clone();
            let ctx_v = ctx2.clone();
            mcx.effect(move || {
                if mode.get() != 1 {
                    return;
                }
                let Some(pair) = picked_pair(
                    &store,
                    &prov_options_v,
                    custom_row,
                    prov_ix,
                    prov_custom,
                    model_ix,
                    model_custom,
                ) else {
                    return;
                };
                if voices_req.get_untracked().as_ref() == Some(&pair) {
                    return;
                }
                voices_req.set(Some(pair.clone()));
                // Re-resolve the picker against the NEW pair's list.
                voice_ix.set(usize::MAX);
                store.voices.set(Loadable::Loading);
                ctx_v.send(Cmd::LoadVoices {
                    provider: pair.0,
                    model: pair.1,
                });
            });

            // Resolve the sentinel once a list arrives: preselect the
            // voice the options JSON carries (fabricated-selection law:
            // no match → the "provider default" placeholder).
            mcx.effect(move || {
                if voice_ix.get() != usize::MAX {
                    return;
                }
                if let Loadable::Ready(d) = store.voices.get() {
                    let saved = options_voice(&options_json.get_untracked());
                    let pos = saved
                        .and_then(|v| d.voices.iter().position(|x| *x == v))
                        .map(|i| i + 1);
                    voice_ix.set(pos.unwrap_or(0));
                }
            });
        }

        super::install_write_done(mcx, &ctx2, form_id, in_flight, form_error, close.clone());

        let prov_select_options: Vec<SelectOption> =
            std::iter::once(SelectOption::new("choose a provider…"))
                .chain(prov_options.iter().map(|p| SelectOption::new(p.clone())))
                .chain(std::iter::once(SelectOption::new(CUSTOM)))
                .collect();

        let prov_options_b = prov_options.clone();
        let prov_options_c = prov_options.clone();
        let prov_options_d = prov_options.clone();
        let row_for_clear = row.clone();
        let ctx_save = ctx2.clone();
        let ctx_clear = ctx2.clone();
        let close_cancel = close.clone();
        let guard_cancel = guard.clone();

        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Route — {} ({})", row2.label, row2.key),
                t0.accent,
            )]))
            .child(applies_now(&t0, &row2))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(efield(
                &t0,
                "Mode",
                // One Tab stop per segment (A1): Tab reaches a segment,
                // Enter/Space or a click picks it; ←/→ are never taken.
                super::w::Segmented::new(MODE_LABELS, Some(mode.get_untracked()))
                    .vertical(true)
                    .bind(mode)
                    .autofocus_chosen(true)
                    .on_pick(move |i| mode.set(i))
                    .view(mcx, &t0),
            ))
            // ---- override controls -------------------------------------
            // Granularity is deliberate: the OUTER region reads only the
            // mode, so the provider/base-url/options fields stay mounted
            // (keeping focus + caret) while model lists arrive; the
            // custom-provider and model rows are their own fine-grained
            // regions.
            .child({
                let ctx_retry = ctx2.clone();
                dyn_view_scoped(LayoutStyle::column().gap(0), move |gcx| {
                    let ctx_retry = ctx_retry.clone();
                    let t = theme.get().tokens;
                    if mode.get() != 1 {
                        return Element::new()
                            .style(LayoutStyle::column())
                            .child(line(vec![span(
                                "  pickers disabled — the engine resolves this route",
                                t.text_faint,
                            )]))
                            .build();
                    }
                    let prov_b = prov_options_b.clone();
                    let prov_vp = prov_b.clone();
                    let ctx_vr = ctx_retry.clone();
                    Element::new()
                        .style(LayoutStyle::column().gap(0))
                        .child(efield(
                            &t,
                            "Provider",
                            Select::new(prov_select_options.clone())
                                .value(prov_ix)
                                .on_change(move |_| {
                                    // The law: a provider switch resets the
                                    // model picker — never a fabricated pair.
                                    model_ix.set(0);
                                    model_custom.set(String::new());
                                })
                                .layout(LayoutStyle::default().w(40).h(1).shrink(0.0))
                                .element(gcx, &t)
                                .build(),
                        ))
                        // Custom-provider name row (only for the custom pick).
                        .child(dyn_view_scoped(LayoutStyle::column(), move |g2| {
                            let t = theme.get().tokens;
                            if prov_ix.get() != custom_row {
                                return Element::new().style(LayoutStyle::default().h(0)).build();
                            }
                            efield(
                                &t,
                                "Provider name",
                                TextInput::new()
                                    .value(prov_custom)
                                    .placeholder("e.g. supertonic, mlx-gen, faster-whisper")
                                    .placeholder_while_focused(true)
                                    .layout(LayoutStyle::default().w(40).h(1))
                                    .element(g2, &t)
                                    .build(),
                            )
                        }))
                        // Model row: list when discovered, honest free text
                        // otherwise.
                        .child(dyn_view_scoped(LayoutStyle::column(), move |g2| {
                            let ctx_retry = ctx_retry.clone();
                            let t = theme.get().tokens;
                            let ix = prov_ix.get();
                            let is_custom = ix == custom_row;
                            if ix == 0 {
                                return efield(
                                    &t,
                                    "Model",
                                    line(vec![span("choose a provider first", t.text_faint)]),
                                );
                            }
                            if is_custom {
                                return efield(
                                    &t,
                                    "Model",
                                    TextInput::new()
                                        .value(model_custom)
                                        .placeholder("model id for that provider")
                                        .placeholder_while_focused(true)
                                        .layout(LayoutStyle::default().w(52).h(1))
                                        .element(g2, &t)
                                        .build(),
                                );
                            }
                            let chosen_name = prov_b[ix - 1].clone();
                            let entry = store
                                .models
                                .with(|m| m.get(&chosen_name).cloned())
                                .unwrap_or(Loadable::NotAsked);
                            match entry {
                                Loadable::Ready(models) if !models.is_empty() => {
                                    let opts: Vec<SelectOption> =
                                        std::iter::once(SelectOption::new("choose a model…"))
                                            .chain(
                                                models.iter().map(|m| SelectOption::new(m.clone())),
                                            )
                                            .collect();
                                    efield(
                                        &t,
                                        "Model",
                                        Combobox::new(opts)
                                            .value(model_ix)
                                            .placeholder("type to filter models…")
                                            .layout(LayoutStyle::default().w(52).h(1).shrink(0.0))
                                            .element(g2, &t)
                                            .build(),
                                    )
                                }
                                Loadable::Loading | Loadable::NotAsked => efield(
                                    &t,
                                    "Model",
                                    line(vec![span("⟳ discovering models…", t.info)]),
                                ),
                                // Discovery FAILED ≠ "endpoint has no
                                // models": show the error verbatim, keep the
                                // honest free-text lane, and offer a REAL
                                // retry (re-committing the same provider is
                                // unobservable — the Select early-returns on
                                // same-value commits, so a "re-pick to
                                // retry" teaching would be a dead gesture).
                                Loadable::Failed(e) => {
                                    let name_btn = chosen_name.clone();
                                    let ctx_btn = ctx_retry.clone();
                                    Element::new()
                                        .style(LayoutStyle::column())
                                        .child(efield(
                                            &t,
                                            "Model",
                                            TextInput::new()
                                                .value(model_custom)
                                                .placeholder("discovery failed — type the model id")
                                                .placeholder_while_focused(true)
                                                .layout(LayoutStyle::default().w(52).h(1))
                                                .element(g2, &t)
                                                .build(),
                                        ))
                                        .child(efield(
                                            &t,
                                            "",
                                            line(vec![span(
                                                format!("discovery failed: {}", e.message),
                                                t.error,
                                            )]),
                                        ))
                                        .child(efield(
                                            &t,
                                            "",
                                            Button::new("Retry model discovery")
                                                .on_click(move || {
                                                    let n = name_btn.clone();
                                                    ctx_btn.store.models.update(|m| {
                                                        drop(m.insert(n.clone(), Loadable::Loading))
                                                    });
                                                    ctx_btn.send(Cmd::LoadModels { provider: n });
                                                })
                                                .element(g2, &t)
                                                .build(),
                                        ))
                                        .build()
                                }
                                Loadable::Ready(_) => efield(
                                    &t,
                                    "Model",
                                    TextInput::new()
                                        .value(model_custom)
                                        .placeholder("no discoverable models — type the model id")
                                        .placeholder_while_focused(true)
                                        .layout(LayoutStyle::default().w(52).h(1))
                                        .element(g2, &t)
                                        .build(),
                                ),
                            }
                        }))
                        // Voice row (output.voice only): a per-pair
                        // catalog picker that writes into the options
                        // JSON — never a second source of truth.
                        .child(dyn_view_scoped(LayoutStyle::column(), move |g2| {
                            let t = theme.get().tokens;
                            if !is_voice {
                                return Element::new().style(LayoutStyle::default().h(0)).build();
                            }
                            let Some(pair) = picked_pair(
                                &store, &prov_vp, custom_row, prov_ix, prov_custom, model_ix,
                                model_custom,
                            ) else {
                                return efield(
                                    &t,
                                    "Voice",
                                    line(vec![span(
                                        "pick provider + model first — voices are per-pair",
                                        t.text_faint,
                                    )]),
                                );
                            };
                            match store.voices.get() {
                                Loadable::Ready(d) if d.provider == pair.0 && d.model == pair.1 => {
                                    if d.voices.is_empty() {
                                        // The reason when the gateway gave
                                        // one (not installed / needs a key).
                                        return efield(
                                            &t,
                                            "Voice",
                                            match &d.unavailable_reason {
                                                Some(why) => {
                                                    let lines = super::util::wrap_text(why, 52);
                                                    let mut col = Element::new().style(
                                                        LayoutStyle::column()
                                                            .h(lines.len() as i32)
                                                            .shrink(0.0),
                                                    );
                                                    for l in lines {
                                                        col = col.child(line(vec![span(l, t.warn)]));
                                                    }
                                                    col.build()
                                                }
                                                None => line(vec![span(
                                                    "no voices reported — the provider default applies",
                                                    t.text_muted,
                                                )]),
                                            },
                                        );
                                    }
                                    let opts: Vec<SelectOption> = std::iter::once(
                                        SelectOption::new("provider default voice"),
                                    )
                                    .chain(d.voices.iter().map(|v| SelectOption::new(v.clone())))
                                    .collect();
                                    let voices_list = d.voices.clone();
                                    efield(
                                        &t,
                                        "Voice",
                                        Combobox::new(opts)
                                            .value(voice_ix)
                                            .placeholder("type to filter voices…")
                                            .on_change(move |ix| {
                                                merge_voice_into_options(
                                                    options_json,
                                                    form_error,
                                                    &voices_list,
                                                    ix,
                                                );
                                            })
                                            .layout(LayoutStyle::default().w(44).h(1).shrink(0.0))
                                            .element(g2, &t)
                                            .build(),
                                    )
                                }
                                Loadable::Failed(e) => {
                                    let ctx_btn = ctx_vr.clone();
                                    let pair_btn = pair.clone();
                                    Element::new()
                                        .style(LayoutStyle::column())
                                        .child(efield(
                                            &t,
                                            "Voice",
                                            line(vec![span(
                                                format!("voice catalog failed: {}", e.message),
                                                t.error,
                                            )]),
                                        ))
                                        .child(efield(
                                            &t,
                                            "",
                                            Button::new("Retry voice catalog")
                                                .on_click(move || {
                                                    voices_req.set(Some(pair_btn.clone()));
                                                    ctx_btn
                                                        .store
                                                        .voices
                                                        .set(Loadable::Loading);
                                                    ctx_btn.send(Cmd::LoadVoices {
                                                        provider: pair_btn.0.clone(),
                                                        model: pair_btn.1.clone(),
                                                    });
                                                })
                                                .element(g2, &t)
                                                .build(),
                                        ))
                                        .child(efield(
                                            &t,
                                            "",
                                            line(vec![span(
                                                "(or type {\"voice\": \"…\"} into options below)",
                                                t.text_faint,
                                            )]),
                                        ))
                                        .build()
                                }
                                _ => efield(
                                    &t,
                                    "Voice",
                                    line(vec![span("⟳ loading voices…", t.info)]),
                                ),
                            }
                        }))
                        .child(efield(
                            &t,
                            "Base URL (optional)",
                            TextInput::new()
                                .value(base_url)
                                .placeholder("inherit from the provider — e.g. http://localhost:1234/v1")
                                .layout(LayoutStyle::default().w(52).h(1))
                                .element(gcx, &t)
                                .build(),
                        ))
                        // Reasoning belongs to TEXT GENERATION only —
                        // the row is absent (not disabled) on every
                        // other route, exactly as the web console hides
                        // it. `not set` clears the stored effort.
                        .child(if is_text {
                            efield(
                                &t,
                                "Reasoning",
                                Select::new(
                                    std::iter::once(SelectOption::new("not set"))
                                        .chain(
                                            crate::store::REASONING_LEVELS
                                                .iter()
                                                .map(|l| SelectOption::new(*l)),
                                        )
                                        .collect::<Vec<_>>(),
                                )
                                .value(reasoning_ix)
                                .layout(LayoutStyle::default().w(28).h(1).shrink(0.0))
                                .element(gcx, &t)
                                .build(),
                            )
                        } else {
                            Element::new().style(LayoutStyle::default().h(0)).build()
                        })
                        .child(efield(
                            &t,
                            "Options (JSON, optional)",
                            TextInput::new()
                                .value(options_json)
                                .placeholder("optional — e.g. {\"voice\": \"M3\"}")
                                .layout(LayoutStyle::default().w(52).h(1))
                                .element(gcx, &t)
                                .build(),
                        ))
                        .child(if is_text {
                            efield(
                                &t,
                                "MTP",
                                Select::new(["inherit", "off", "depth 2", "depth 3", "depth 4", "depth 5", "custom (JSON)"]
                                    .iter().map(|label| SelectOption::new(*label)).collect::<Vec<_>>())
                                    .value(speculation_ix)
                                    .on_change(move |ix| {
                                        match speculation_options(&spec_json.get_untracked(), ix) {
                                            Ok(value) => { spec_json.set(value); form_error.set(None); }
                                            Err(error) => form_error.set(Some(error)),
                                        }
                                    })
                                    .layout(LayoutStyle::default().w(28).h(1).shrink(0.0))
                                    .element(gcx, &t).build(),
                            )
                        } else {
                            Element::new().style(LayoutStyle::default().h(0)).build()
                        })
                        .build()
                })
            })
            .child(super::message_slot(theme, form_error, in_flight))
            // Test outcome: a REAL generation through the production lane
            // (voice → tts run, others → sandbox with this route's key).
            .child(dyn_view(
                LayoutStyle::default().h(2).shrink(0.0),
                move || {
                    let t = theme.get().tokens;
                    match store.route_test.get() {
                        Loadable::NotAsked => line(vec![span(String::new(), t.text)]),
                        Loadable::Loading => line(vec![span(
                            "⟳ testing — a real generation through the production lane (up to ~25s)…",
                            t.info,
                        )]),
                        Loadable::Failed(e) => line(vec![span_bold(
                            format!("✗ test failed: {}", e.message),
                            t.error,
                        )]),
                        Loadable::Ready(o) => Element::new()
                            .style(LayoutStyle::column())
                            .child(line(vec![if o.ok {
                                span_bold(format!("✓ {}", o.summary), t.ok)
                            } else {
                                span_bold(format!("✗ {}", o.summary), t.error)
                            }]))
                            .child(line(vec![span(
                                format!("  {}", o.detail.clone().unwrap_or_default()),
                                t.text_muted,
                            )]))
                            .build(),
                    }
                },
            ))
            .child(dyn_view_scoped(
                LayoutStyle::line(1).shrink(0.0),
                move |gcx| {
                    let t = theme.get().tokens;
                    let overriding = mode.get() == 1;
                    let ix = prov_ix.get();
                    let is_custom = ix == custom_row;
                    let provider_ok = if is_custom {
                        !prov_custom.get().trim().is_empty()
                    } else {
                        ix > 0
                    };
                    let model_ok = if is_custom {
                        !model_custom.get().trim().is_empty()
                    } else if ix > 0 {
                        let name = prov_options_c[ix - 1].clone();
                        let has_list = store.models.with(|m| {
                            m.get(&name)
                                .and_then(|l| l.ready())
                                .map(|models| !models.is_empty())
                                .unwrap_or(false)
                        });
                        if has_list {
                            let mix = model_ix.get();
                            mix != 0 && mix != usize::MAX
                        } else {
                            !model_custom.get().trim().is_empty()
                        }
                    } else {
                        false
                    };
                    let busy_form = in_flight.get();
                    let testing = matches!(store.route_test.get(), Loadable::Loading);
                    let clear_enabled = !overriding
                        && row_for_clear.configured
                        && row_for_clear.covered_by.is_none()
                        && !busy_form;

                    // Per-run clones: this closure is FnMut and re-runs — the
                    // buttons must never consume the captured originals.
                    let row3 = row_for_clear.clone();
                    let row4 = row_for_clear.clone();
                    let row_t = row_for_clear.clone();
                    let ctx_s = ctx_save.clone();
                    let ctx_c = ctx_clear.clone();
                    let ctx_t = ctx_save.clone();
                    let prov_options_e = prov_options_d.clone();
                    let prov_options_t = prov_options_d.clone();
                    let close_b = close_cancel.clone();
                    let close_after_clear = close_cancel.clone();
                    let save_why = if !overriding {
                        Err("pick \"override\" first — the default mode has nothing to save".to_string())
                    } else if !(provider_ok && model_ok) {
                        Err("choose a provider and model first".to_string())
                    } else if busy_form {
                        Err("saving…".to_string())
                    } else {
                        Ok(())
                    };
                    let test_why = if !overriding || !(provider_ok && model_ok) {
                        Err("pick provider + model first".to_string())
                    } else if testing {
                        Err("a test is running".to_string())
                    } else {
                        Ok(())
                    };
                    let clear_why = if clear_enabled {
                        Ok(())
                    } else if overriding {
                        Err("pick \"use default\" first — Clear removes the override".to_string())
                    } else {
                        Err("this route has no override to clear".to_string())
                    };
                    let mut on_save: Box<dyn FnMut()> = Box::new(move || {
                                    if in_flight.get_untracked() {
                                        return; // a write is already running
                                    }
                                    // ONE pick derivation — the same helper the
                                    // Test button and the readiness line use
                                    // (the old inline copy diverged once:
                                    // unwrap_or_default() could PUT model:"").
                                    // save_enabled gates on the pair being
                                    // ready, so None here is only a race.
                                    let Some((provider, model)) = picked_pair(
                                        &store,
                                        &prov_options_e,
                                        custom_row,
                                        prov_ix,
                                        prov_custom,
                                        model_ix,
                                        model_custom,
                                    ) else {
                                        form_error.set(Some(
                                            "choose a provider and model first".into(),
                                        ));
                                        return;
                                    };
                                    let reasoning = is_text.then(|| {
                                        crate::store::REASONING_LEVELS
                                            .get(reasoning_ix.get_untracked().wrapping_sub(1))
                                            .map(|s| (*s).to_string())
                                            .unwrap_or_default()
                                    });
                                    let options = match compose_route_options(
                                        &options_json.get_untracked(),
                                        &spec_json.get_untracked(),
                                        is_text,
                                    ) {
                                        Ok(o) => o,
                                        Err(e) => {
                                            form_error.set(Some(e));
                                            return;
                                        }
                                    };
                                    let body = match route_save_body(
                                        &provider,
                                        &model,
                                        reasoning.as_deref(),
                                        (&base_url.get_untracked(), row3.base_url.as_deref().unwrap_or("")),
                                        (&options, &shown_options(&row3)),
                                    ) {
                                        Ok(body) => body,
                                        Err(e) => {
                                            form_error.set(Some(e));
                                            return;
                                        }
                                    };
                                    form_error.set(None);
                                    in_flight.set(true);
                                    ctx_s.send(Cmd::PutRoute {
                                        kind: row3.kind.clone(),
                                        modality: row3.modality.clone(),
                                        task: row3.task.clone(),
                                        body: body.into(),
                                        key: row3.key.clone(),
                                        form_id: Some(form_id),
                                    });
                                });
                    let mut on_test: Box<dyn FnMut()> = Box::new(move || {
                                    let Some((provider, model)) = picked_pair(
                                        &ctx_t.store,
                                        &prov_options_t,
                                        custom_row,
                                        prov_ix,
                                        prov_custom,
                                        model_ix,
                                        model_custom,
                                    ) else {
                                        form_error
                                            .set(Some("pick provider + model first".into()));
                                        return;
                                    };
                                    let voice = if row_t.key == "output.voice" {
                                        options_voice(&options_json.get_untracked())
                                    } else {
                                        None
                                    };
                                    let mut controls = json!({});
                                    if row_t.is_text_generation() {
                                        let text = match compose_route_options(
                                            &options_json.get_untracked(),
                                            &spec_json.get_untracked(),
                                            true,
                                        ) {
                                            Ok(t) => t,
                                            Err(e) => { form_error.set(Some(e)); return; }
                                        };
                                        let parsed = if text.trim().is_empty() { Ok(json!({})) } else { serde_json::from_str::<Value>(&text) };
                                        let options = match parsed {
                                            Ok(value) if value.is_object() => value,
                                            _ => { form_error.set(Some("options must be a JSON object before testing".into())); return; }
                                        };
                                        if let Some(value) = options.get("speculation") {
                                            controls["speculation"] = value.clone();
                                            if value.is_object() && value.get("mode").and_then(Value::as_str) != Some("off") {
                                                controls["speculation"]["require_acceleration"] = json!(true);
                                            }
                                        }
                                        if let Some(level) = crate::store::REASONING_LEVELS.get(reasoning_ix.get_untracked().wrapping_sub(1)) {
                                            controls["reasoning"] = json!(level);
                                        }
                                    }
                                    ctx_t.store.route_test.set(Loadable::Loading);
                                    ctx_t.send(Cmd::TestRoute {
                                        key: row_t.key.clone(),
                                        provider,
                                        model,
                                        voice,
                                        controls,
                                        voice_run_id: voice_test_run_id(&ctx_t.store),
                                    });
                                });
                    let on_clear = move || {
                        close_after_clear();
                        confirm_clear(screen_cx, &ctx_c, row4.clone());
                    };
                    let guard_c = guard_cancel.clone();
                    let on_cancel = move || {
                        // Cancel asks first when edits are unsaved (R15 F2).
                        let handled = guard_c.borrow().as_ref().map(|g| g()).unwrap_or(false);
                        if !handled {
                            close_b();
                        }
                    };
                    let mut on_cancel = Some(on_cancel);
                    let mut on_clear = Some(on_clear);
                    let mut views = Vec::new();
                    for a in editor_actions(save_why, test_why, clear_why) {
                        let v = match a.id {
                            "cancel" => {
                                let f = on_cancel.take().expect("once");
                                button(gcx, &t, &a, On::Raised, true, f)
                            }
                            "clear" => {
                                let f = on_clear.take().expect("once");
                                button(gcx, &t, &a, On::Raised, true, f)
                            }
                            "test" => {
                                let f = std::mem::replace(&mut on_test, Box::new(|| {}) as Box<dyn FnMut()>);
                                button(gcx, &t, &a, On::Raised, true, f)
                            }
                            _ => {
                                let f = std::mem::replace(&mut on_save, Box::new(|| {}) as Box<dyn FnMut()>);
                                button(gcx, &t, &a, On::Raised, true, f)
                            }
                        };
                        views.push(v);
                    }
                    super::w::form::button_row(views)
                },
            ))
            .build()
    });
}

#[cfg(test)]
mod speculation_tests {
    use super::*;

    /// The voice test run id folds like the web's `voiceTestRunId`.
    #[test]
    fn voice_test_run_id_folds_like_the_web() {
        assert_eq!(
            super::voice_test_run_id_for("a:b", "John..Doe"),
            "session_memory_gateway_console_voicetest_a_b_john_doe"
        );
        assert_eq!(
            super::voice_test_run_id_for("", ""),
            "session_memory_gateway_console_voicetest_default_user"
        );
    }

    /// The web's route editor (review 2 minor g): a text route's options
    /// box hides `speculation` (the MTP selector owns it) and refuses it
    /// typed there; an untouched editor saves exactly what was stored.
    #[test]
    fn options_box_hides_and_refuses_speculation_on_text_routes() {
        let stored = json!({"temperature": 0.2, "speculation": false});
        let (shown, spec) = split_route_options(Some(&stored), true);
        assert_eq!(shown, r#"{"temperature":0.2}"#);
        assert_eq!(spec, r#"{"speculation":false}"#);
        assert_eq!(
            compose_route_options(&shown, &spec, true).unwrap(),
            stored.to_string(),
            "unchanged editor = the stored options, so the save omits them"
        );
        let err = compose_route_options(r#"{"speculation":true}"#, &spec, true).unwrap_err();
        assert!(
            err.starts_with("Use the MTP selector for speculation"),
            "{err}"
        );
        // Other routes show and send every option.
        let voice = json!({"voice": "M3", "speculation": 1});
        let (shown, spec) = split_route_options(Some(&voice), false);
        assert_eq!(shown, voice.to_string());
        assert_eq!(spec, "");
        assert_eq!(
            compose_route_options(&shown, "", false).unwrap(),
            voice.to_string()
        );
    }

    #[test]
    fn selector_keeps_off_distinct_from_inherit_and_preserves_options() {
        let off: Value =
            serde_json::from_str(&speculation_options(r#"{"temperature":0.2}"#, 1).unwrap())
                .unwrap();
        assert_eq!(off["speculation"], false);
        assert_eq!(off["temperature"], 0.2);
        assert_eq!(speculation_index(&off), 1);
        let inherited: Value =
            serde_json::from_str(&speculation_options(&off.to_string(), 0).unwrap()).unwrap();
        assert!(inherited.get("speculation").is_none());
        assert_eq!(inherited["temperature"], 0.2);
    }

    #[test]
    fn depth_edit_preserves_matching_head_and_uses_optional_default_policy() {
        let value: Value = serde_json::from_str(
            &speculation_options(
                r#"{"speculation":{"drafter":"matching/head","num_draft_tokens":2},"seed":7}"#,
                4,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(value["speculation"]["num_draft_tokens"], 4);
        assert_eq!(value["speculation"]["drafter"], "matching/head");
        assert_eq!(value["speculation"]["require_acceleration"], false);
        assert_eq!(value["seed"], 7);
        assert_eq!(speculation_index(&value), 4);
    }

    #[test]
    fn malformed_and_custom_options_are_not_silently_overwritten() {
        assert!(speculation_options("{", 2).is_err());
        assert!(speculation_options("[]", 2).is_err());
        let input = json!({"speculation":{"mode":"native_mtp","num_draft_tokens":7}});
        assert_eq!(speculation_index(&input), 6);
        assert_eq!(
            serde_json::from_str::<Value>(&speculation_options(&input.to_string(), 6).unwrap())
                .unwrap(),
            input
        );
    }

    /// The route save rule, the web's saveDefault: untouched base URL /
    /// options are NOT named; edited or emptied ones ARE ("" / {}); the
    /// text route's reasoning is always explicit; bad options refuse.
    #[test]
    fn route_save_body_sends_what_the_operator_changed() {
        let opts = r#"{"temperature":0.7}"#;
        let untouched = route_save_body(
            "lmstudio",
            "m",
            None,
            ("http://h/v1", "http://h/v1"),
            (opts, opts),
        )
        .unwrap();
        assert_eq!(untouched, json!({"provider": "lmstudio", "model": "m"}));
        let cleared =
            route_save_body("lmstudio", "m", None, ("", "http://h/v1"), ("", opts)).unwrap();
        assert_eq!(cleared["base_url"], "", "an emptied base URL is sent empty");
        assert_eq!(
            cleared["options"],
            json!({}),
            "emptied options are sent as {{}}"
        );
        let edited =
            route_save_body("p", "m", Some(""), (" http://x ", ""), (r#"{"a":1}"#, "")).unwrap();
        assert_eq!(edited["base_url"], "http://x", "trimmed");
        assert_eq!(edited["options"], json!({"a": 1}));
        assert_eq!(edited["reasoning"], "", "text route: explicit, \"\" clears");
        let with_reasoning = route_save_body("p", "m", Some("high"), ("", ""), ("", "")).unwrap();
        assert_eq!(
            with_reasoning,
            json!({"provider": "p", "model": "m", "reasoning": "high"})
        );
        // Validated even when untouched: a stored typo never rides along.
        assert!(route_save_body("p", "m", None, ("", ""), ("[1]", "[1]")).is_err());
        assert!(route_save_body("p", "m", None, ("", ""), ("{nope", "")).is_err());
    }
}

/// How the transcription line reads (its ink).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TranscriptionLevel {
    Ready,
    Unset,
    Warn,
    Error,
}

/// The transcription (speech → text, `input.voice`) line: the engine by
/// the name the payload carries (`engine_missing.name` when Core judged
/// the engine, else the route's provider id) and the model, then the
/// state — "Engine missing: <Core's reason>" only when the row carries
/// `engine_missing`. None when the gateway serves no `input.voice` row.
pub fn transcription_line(rows: &[RouteRow]) -> Option<(String, TranscriptionLevel)> {
    let r = rows.iter().find(|r| r.key == "input.voice")?;
    let model = r.model.clone().unwrap_or_default();
    let pair = |engine: &str| {
        if model.is_empty() {
            engine.to_string()
        } else {
            format!("{engine} · {model}")
        }
    };
    let provider = r.provider.clone().unwrap_or_default();
    if let Some(by) = &r.covered_by {
        return Some((
            format!("(speech → text): served by {by} — {}", pair(&provider)),
            TranscriptionLevel::Ready,
        ));
    }
    if r.configured {
        if let Some(u) = &r.route_unavailable {
            return Some((
                format!(
                    "(speech → text): {} — cannot run on this computer: {}",
                    pair(&provider),
                    u.reason
                ),
                TranscriptionLevel::Error,
            ));
        }
        if let Some(m) = &r.engine_missing {
            let engine = if m.name.trim().is_empty() {
                provider.as_str()
            } else {
                m.name.as_str()
            };
            let fix = match &m.install {
                Some(cmd) => format!(" — install: {cmd}"),
                None => " — pick a transcription engine (Enter on input.voice)".to_string(),
            };
            return Some((
                format!(
                    "(speech → text): {} — Engine missing: {}{fix}",
                    pair(engine),
                    m.reason
                ),
                TranscriptionLevel::Warn,
            ));
        }
        return Some((
            format!("(speech → text): {} — ready", pair(&provider)),
            TranscriptionLevel::Ready,
        ));
    }
    if let Some(u) = &r.recommendation_unavailable {
        return Some((
            format!(
                "(speech → text): not set — the recommended {} cannot run on this computer: {}",
                u.pair_text(),
                u.reason
            ),
            TranscriptionLevel::Warn,
        ));
    }
    Some((
        "(speech → text): not set — pick a transcription engine (Enter on input.voice)".to_string(),
        TranscriptionLevel::Unset,
    ))
}
