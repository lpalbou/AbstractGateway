//! Multimodal capability routes: the default-vs-override table.
//!
//! The fabricated-selection law (2026-07-17 incident) is implemented
//! here: route editors have an explicit Default-vs-Override mode,
//! placeholder items occupy index 0 of every picker, pick controls are
//! disabled in default mode, the resolved state renders as an
//! "Applies now: …" line derived only from server state, and switching
//! provider resets the model picker — never a fabricated pair.

use abstracttui::prelude::*;
use abstracttui::widgets::Table;
use serde_json::{json, Value};

use super::util::{field, line, loadable_view, or_dash, span, span_bold};
use super::widths;
use super::widths::BLOCK_CHROME;
use super::Ctx;
use crate::store::{ConnPhase, Loadable, RouteRow, RoutesData, WeightsRow};
use crate::worker::Cmd;

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let viewport = abstracttui::app::use_viewport(cx);

    super::util::clamp_selection(cx, ui.route_sel, move || {
        store
            .routes
            .with(|d| d.ready().map(|d| d.rows.len()).unwrap_or(0))
    });

    let ctx_edit = ctx.clone();
    let ctx_edit2 = ctx.clone();
    let ctx_clear = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            edit_selected(cx, &ctx_edit);
        })
        .shortcut(KeyChord::plain(Key::Enter), move |_| {
            edit_selected(cx, &ctx_edit2);
        })
        .shortcut(KeyChord::plain(Key::Char('x')), move |_| {
            clear_selected(cx, &ctx_clear);
        })
        // `w` for WEIGHTS, and deliberately not `d`. `d` deletes on the
        // Providers and Users screens, and a key that means "delete" two
        // screens over must not mean "download" here: downloads are safe and
        // frequent, so `d` would train a fast confirm-and-move-on reflex that
        // an operator then carries onto a destructive prompt. `w` also names
        // the column the operator is looking at.
        .shortcut(KeyChord::plain(Key::Char('w')), {
            let ctx_dl = ctx.clone();
            move |_| download_selected(cx, &ctx_dl)
        })
        // `a` for APPLY — the other half of the weights banner, and the
        // same key, prompt and vocabulary as the AbstractCore console-TUI
        // and the gateway console's "Apply recommended" button. Safe by
        // default: replacing routes the operator configured is a separate,
        // danger-tinted answer, never the accidental one.
        .shortcut(KeyChord::plain(Key::Char('a')), {
            let ctx_apply = ctx.clone();
            move |_| apply_recommended(cx, &ctx_apply)
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Routes — which provider & model serve each input/output")
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1)),
                )
                .child(dyn_view(LayoutStyle::column(), move || {
                    let t = tt;
                    match store.routes.get() {
                        Loadable::Ready(d) => {
                            // The authority MODULE NAME is internal noise
                            // in the healthy case (P2-A) — surface it only
                            // in the read-only/error state, where it is
                            // actionable ("which backend is refusing").
                            let spans = if d.writable {
                                // `a` rides the ALWAYS-PRESENT banner, not the
                                // weights line below: that line fills with a
                                // missing-artifact list and truncates anything
                                // appended to it.
                                vec![
                                    span("writable", t.ok),
                                    span(
                                        "  ·  a applies the recommended routes".to_string(),
                                        t.text_faint,
                                    ),
                                ]
                            } else {
                                vec![
                                    span_bold("read-only (backend unreachable?)", t.warn),
                                    span(format!("  ·  route store {}", d.authority), t.text_faint),
                                ]
                            };
                            let mut col = Element::new()
                                .style(LayoutStyle::column())
                                .child(line(spans));
                            if !d.ok {
                                // Errors get their own full-width line —
                                // appended to the banner they clip away.
                                col = col.child(line(vec![span_bold(
                                    format!("gateway reports errors: {}", d.errors.join(" | ")),
                                    t.error,
                                )]));
                            }
                            col.build()
                        }
                        _ => line(vec![span(String::new(), t.text)]),
                    }
                }))
                // WEIGHTS BANNER. A route with NOTHING serving it cannot
                // run, and the framework's recommended model for it is the
                // one-click way out — invisible in the grid above, which is
                // the single most common fresh-install confusion. This line
                // names those routes and nothing else.
                //
                // IT USED TO READ "recommended models: 2 of 3 present ·
                // missing: lmstudio qwen/qwen3.5-9b@4bit" ON A FULLY
                // CONFIGURED HOST, in warn amber, forever: the operator had
                // routed text generation at their own model, so the starter
                // kit's build was absent and would stay absent. A warning
                // whose only cure is installing the model you chose against
                // is not a warning, it is noise — and noise on the healthy
                // path is how an operator learns to skip the line that
                // matters. The gateway now decides which recommended models
                // belong to an UNANSWERED route (`recommended.gaps`), and a
                // host with none gets no banner at all.
                //
                // THE MISSING LIST IS THE ELASTIC PART. It can name a
                // dozen artifacts; the verb after it is the only
                // actionable text on the row, and `line()` clips
                // last-span-first — which is how the operator saw
                // `w downloads the selecte…`. So the verb (and the count,
                // and the `missing:` label) are reserved, the list gets
                // what is left, and it keeps its TAIL: the `@4bit`-style
                // tag is what distinguishes one absent artifact from
                // another.
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    let t = tt;
                    let avail = viewport.get().w - BLOCK_CHROME;
                    if let Some(dl) = store.download.get() {
                        // A live download outranks the summary: it is the
                        // thing that is happening right now.
                        let tone = if dl.running() {
                            t.info
                        } else if dl.status == "completed" {
                            t.ok
                        } else {
                            t.error
                        };
                        return line(vec![span_bold(format!("download: {}", dl.line()), tone)]);
                    }
                    match store.availability.get() {
                        Loadable::Ready(a) if !a.missing.is_empty() => {
                            let head = format!(
                                "{} route{} with no model yet",
                                a.missing.len(),
                                if a.missing.len() == 1 { "" } else { "s" }
                            );
                            let routes = a
                                .missing
                                .iter()
                                .map(|(route, _, _)| route.as_str())
                                .filter(|route| !route.is_empty())
                                .collect::<Vec<_>>()
                                .join(", ");
                            let routes = if routes.is_empty() {
                                String::new()
                            } else {
                                format!("  ·  {routes}")
                            };
                            let mut spans = vec![
                                span_bold(head.clone(), t.warn),
                                span(routes.clone(), t.text),
                            ];
                            const LABEL: &str = "  ·  recommended: ";
                            const VERB: &str = "  ·  w downloads the selected route's weights";
                            let list = a
                                .missing
                                .iter()
                                .map(|(_, p, art)| format!("{p} {art}"))
                                .collect::<Vec<_>>()
                                .join(", ");
                            let budget =
                                widths::elastic_budget(&[&head, &routes, LABEL, VERB], avail);
                            // Two honest lines, widest first: name the
                            // artifacts, or say nothing about them — but
                            // never at the verb's expense, and never by
                            // clipping a word. The COUNT is already in the
                            // head, so there is no third form to fall back on.
                            if budget >= 8 {
                                spans.push(span(
                                    format!("{LABEL}{}", widths::middle_fit(&list, budget)),
                                    t.warn,
                                ));
                            }
                            spans.push(span(VERB.to_string(), t.text_faint));
                            line(spans)
                        }
                        Loadable::Failed(e) => line(vec![span(
                            format!("model availability unavailable: {e}"),
                            t.text_muted,
                        )]),
                        _ => line(vec![span(String::new(), t.text)]),
                    }
                }))
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    let ctx_act = ctx.clone();
                    move |gcx| {
                        let data = store.routes.get();
                        let ctx_act = ctx_act.clone();
                        loadable_view(
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |d: &RoutesData| d.rows.is_empty(),
                            "the gateway reported no routes",
                            |d| {
                                let weights = store.availability.with(|a| {
                                    a.ready().map(|a| a.by_route.clone()).unwrap_or_default()
                                });
                                routes_table(gcx, &tt, d, &weights, ui.route_sel, move |_| {
                                    // Activation = the Enter/e path, one
                                    // body (per-row editability refusals
                                    // included). The screen-level Enter
                                    // shortcut stays as the empty-table
                                    // fallback: with rows, the focused
                                    // table consumes Enter first.
                                    edit_selected(cx, &ctx_act);
                                })
                            },
                        )
                    }
                }))
                // SELECTED-ROW LINE. Two jobs, both of which the columns
                // cannot do: keep the FULL route key readable (the route
                // column now shows a task row as `└ text_to_image` under
                // its parent) and say in words what a parent row is FOR.
                // The grid alone made an operator ask whether
                // `output.image` was dead code sitting above t2i / i2i /
                // upscale; it is the opposite — the ONE value that serves
                // every image task with no row of its own, and the simple
                // setting for someone who wants one image model.
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    let t = tt;
                    let row: Option<RouteRow> = store.routes.with(|d| {
                        d.ready()
                            .and_then(|d| d.rows.get(ui.route_sel.get()).cloned())
                    });
                    match row {
                        Some(r) => {
                            let mut spans = vec![span_bold(format!(" {} ", r.key), t.accent)];
                            if r.is_task_parent() {
                                spans.push(span(
                                    format!(
                                        "serves any {} task with no row of its own  ",
                                        r.modality
                                    ),
                                    t.text_muted,
                                ));
                                if r.covered_by_tasks {
                                    spans.push(span(
                                        format!(
                                            "· all {} task rows below are set, so nothing reads it",
                                            r.task_keys.len()
                                        ),
                                        t.text_faint,
                                    ));
                                }
                            } else if let Some(parent) = &r.broad_key {
                                spans.push(span(
                                    if r.inherits_broad {
                                        format!("no value of its own — {parent} answers it  ")
                                    } else {
                                        format!("overrides {parent}  ")
                                    },
                                    t.text_muted,
                                ));
                            }
                            line(spans)
                        }
                        None => line(vec![span(String::new(), t.text)]),
                    }
                }))
                .element(t)
                .build(),
        )
        .build()
}

fn routes_table(
    cx: Scope,
    t: &TokenSet,
    data: &RoutesData,
    weights: &std::collections::HashMap<String, WeightsRow>,
    sel: Signal<usize>,
    on_activate: impl FnMut(usize) + 'static,
) -> View {
    // Width-aware columns (0900 class): which columns APPEAR is a
    // breakpoint decision (source drops first, provider second); how wide
    // the survivors are is MEASURED from the rows themselves by
    // `ui::widths`, never spent as a constant. The old policy sized the
    // grid from `Cells(28)`/`Cells(20)` plus a pre-render
    // `ellipsize(model, 40)` and handed the leftover to a Flex model
    // column — which is how a 200-cell terminal printed
    // `AbstractFramework/wan2.2-t2v-a14b-diffu…` beside seventy blank
    // cells, with the `t2v`/`i2v` that told the two rows apart cut off.
    let w = abstracttui::app::use_viewport(cx).get().w;
    let mut rows: Vec<Vec<String>> = data
        .rows
        .iter()
        .map(|r| {
            // THE shared state vocabulary, straight off the row model —
            // the same four strings the AbstractCore console prints.
            let state = r.state_label();
            // `⊘` U+2298, NOT `🔒` U+1F512 (the sibling console's glyph
            // research, adopted here): the padlock is Emoji=Yes and
            // measures 2 cells, and terminals/fonts routinely draw it at
            // a different advance than the engine measured — the row's
            // later columns then slide and overlap. U+2298 is width 1
            // under BOTH unicode-width conventions, in a block
            // emoji-data never touches. The `state` column already
            // spells the reason in words, so the glyph is garnish.
            let lock = if !r.editable() { " ⊘" } else { "" };
            // THE ROUTE COLUMN CARRIES THE HIERARCHY. `output.image` is
            // the PARENT of `output.image.*` — one value for every image
            // task, overridden per task by the rows beneath it. Printed
            // as four flat siblings with the parent on top reading "not
            // configured", it looked like a leftover key ("why do we have
            // output.image AND t2i/i2i/upscale?"). `display_key()` indents
            // the children under a tree marker and drops the repeated
            // parent prefix; the parent names what it is for.
            let mut row = vec![format!("{}{}", r.display_key(), lock), state];
            if w >= 96 {
                row.push(or_dash(&r.provider));
            }
            // The FULL model name — the column solver sizes to it and
            // cuts it only when the terminal cannot carry it. A constant
            // cap here truncated the payload while the space to print it
            // whole sat unused one column over.
            row.push(or_dash(&r.model));
            // WEIGHTS: is this route's model actually on the execution
            // host? Blank while unprobed and for rows that name no model
            // — the absence of an answer must not read as an answer.
            row.push(
                weights
                    .get(&r.key)
                    .map(|w| w.label().to_string())
                    .unwrap_or_default(),
            );
            if w >= 112 {
                row.push(r.source.clone());
            }
            row
        })
        .collect();
    // The rules carry a FLOOR, not a width: nothing is capped while the
    // terminal has room. Route keys, provider ids, model artifacts and
    // source modules all discriminate on their TAIL
    // (`…image_to_scene3d`, `…-t2v-a14b-diffusers-8bit`), so those cut in
    // the middle. `state` and `weights` print CLOSED VOCABULARIES, so
    // their floor is the widest word each can say — a squeezed vocabulary
    // column is not a shorter answer, it is a different (wrong) one; the
    // open columns take the squeeze on its behalf.
    let mut rules = vec![
        widths::ColRule::tail("route", 18),
        widths::ColRule::head("state", 21),
    ];
    if w >= 96 {
        rules.push(widths::ColRule::tail("provider", 10));
    }
    rules.push(widths::ColRule::tail("model", 22));
    // The weights column earns its floor at every width: "configured
    // but not downloaded" is the single most common reason a route that
    // LOOKS right does not run, and hiding it on a narrow terminal hides
    // it on exactly the machine most likely to be a fresh install. 14
    // cells is the widest label it prints ("not downloaded"), so the
    // floor is the whole vocabulary, never a stub.
    rules.push(widths::ColRule::head("weights", 14));
    if w >= 112 {
        rules.push(widths::ColRule::tail("source", 12));
    }
    // This grid lives inside the screen's bordered block, which spends one
    // cell on each side (measured against the live gateway: a 200-cell
    // terminal gives the table 198). The core console's routes screen
    // mounts bare in PageHost's page region and passes the viewport
    // straight through — one policy, per-screen chrome.
    let cols = widths::columns(&rules, &mut rows, w - BLOCK_CHROME);
    Table::new(cols)
        .rows(rows)
        .selection(sel)
        .on_activate(on_activate)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .autofocus()
        .build()
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
    let ctx_keep = ctx.clone();
    let ctx_force = ctx.clone();
    let prompt = abstracttui::app::ChoicePrompt::new(
        "Apply the framework's recommended routes (text, voice, image) on the execution host?"
            .to_string(),
    )
    .option("keep", "Apply — keep routes I configured")
    .option_with(
        abstracttui::app::ChoiceOption::new("force", "Apply — replace mine too").danger(true),
    )
    .option("cancel", "Cancel")
    .initial("keep");
    super::open_prompt(cx, ctx.ui, prompt, move |outcome| {
        if let abstracttui::app::ChoiceOutcome::Answered(a) = outcome {
            let choice = a.selected.first().cloned().unwrap_or_default();
            let (ctx2, force) = match choice.as_str() {
                "keep" => (ctx_keep, false),
                "force" => (ctx_force, true),
                _ => return,
            };
            ctx2.send(Cmd::ApplyRecommendedRoutes { force });
        }
    });
}

/// `d` — download the selected route's model weights on the execution
/// host.
///
/// NEVER AUTOMATIC, ALWAYS CONFIRMED, and word-for-word the refusals the
/// AbstractCore console gives for the same states: an installed model, a
/// relay provider with nothing to fetch, and — the important one — an
/// `unknown` answer, where guessing would spend the host's disk on a
/// model that may already be there.
fn download_selected(cx: Scope, ctx: &Ctx) {
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
            ctx.store.notice.set(Some(format!(
                "{} is already installed{}",
                weights.artifact,
                if weights.detail.is_empty() {
                    String::new()
                } else {
                    format!(" ({})", weights.detail)
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
            ctx.store.notice.set(Some(format!(
                "{}: availability is unknown — {hint}",
                row.key
            )));
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

    let ctx2 = ctx.clone();
    let provider = weights.provider.clone();
    let artifact = weights.artifact.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Download {artifact} with {provider} on the execution host? This runs the \
             provider's own tool and may fetch several gigabytes."
        ),
        "Download",
        "Not now",
        move || {
            ctx2.send(Cmd::DownloadModel {
                provider: provider.clone(),
                artifact: artifact.clone(),
            });
            // The worker lane is SERIAL, so this re-probe runs after the
            // job finishes — the weights column tells the truth the
            // moment the operator looks back at it.
            ctx2.send(Cmd::LoadAvailability);
        },
    );
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
        "Clear the route",
        "Keep the override",
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
    fn sanitize(s: &str, fallback: &str) -> String {
        let out: String = s
            .to_lowercase()
            .chars()
            .map(|c| {
                if c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, ':' | '-' | '_') {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        let out = out.trim_matches('_').to_string();
        if out.is_empty() {
            fallback.to_string()
        } else {
            out
        }
    }
    format!(
        "session_memory_gateway_console_voicetest_{}_{}",
        sanitize(&tenant, "default"),
        sanitize(&user, "user")
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

    super::open_form_guarded(ctx, cx, Size::new(78, 27), move |mcx, close, guard| {
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
        let options_json = mcx.signal(
            row.options
                .as_ref()
                .map(|o| o.to_string())
                .unwrap_or_default(),
        );
        let speculation_ix = mcx.signal(speculation_index(
            row.options.as_ref().unwrap_or(&Value::Null),
        ));
        mcx.effect(move || {
            if let Ok(options) = serde_json::from_str::<Value>(&options_json.get()) {
                speculation_ix.set(speculation_index(&options));
            } else if options_json.get().trim().is_empty() {
                speculation_ix.set(0);
            }
        });
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

        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold(
                format!("Route — {} ({})", row2.label, row2.key),
                t0.accent,
            )]))
            .child(applies_now(&t0, &row2))
            .child(line(vec![span(String::new(), t0.text)]))
            .child(field(
                &t0,
                "mode",
                RadioGroup::new(vec![
                    "use default (engine decides)".to_string(),
                    "override: pick provider + model".to_string(),
                ])
                .selection(mode)
                .element(mcx, &t0)
                .autofocus()
                .build(),
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
                        .child(field(
                            &t,
                            "provider",
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
                            field(
                                &t,
                                "provider name",
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
                                return field(
                                    &t,
                                    "model",
                                    line(vec![span("choose a provider first", t.text_faint)]),
                                );
                            }
                            if is_custom {
                                return field(
                                    &t,
                                    "model",
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
                                    field(
                                        &t,
                                        "model",
                                        Combobox::new(opts)
                                            .value(model_ix)
                                            .placeholder("type to filter models…")
                                            .layout(LayoutStyle::default().w(52).h(1).shrink(0.0))
                                            .element(g2, &t)
                                            .build(),
                                    )
                                }
                                Loadable::Loading | Loadable::NotAsked => field(
                                    &t,
                                    "model",
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
                                        .child(field(
                                            &t,
                                            "model",
                                            TextInput::new()
                                                .value(model_custom)
                                                .placeholder("discovery failed — type the model id")
                                                .placeholder_while_focused(true)
                                                .layout(LayoutStyle::default().w(52).h(1))
                                                .element(g2, &t)
                                                .build(),
                                        ))
                                        .child(field(
                                            &t,
                                            "",
                                            line(vec![span(
                                                format!("discovery failed: {}", e.message),
                                                t.error,
                                            )]),
                                        ))
                                        .child(field(
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
                                Loadable::Ready(_) => field(
                                    &t,
                                    "model",
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
                                return field(
                                    &t,
                                    "voice",
                                    line(vec![span(
                                        "pick provider + model first — voices are per-pair",
                                        t.text_faint,
                                    )]),
                                );
                            };
                            match store.voices.get() {
                                Loadable::Ready(d) if d.provider == pair.0 && d.model == pair.1 => {
                                    if d.voices.is_empty() {
                                        return field(
                                            &t,
                                            "voice",
                                            line(vec![span(
                                                "no voices reported — the provider default applies",
                                                t.text_muted,
                                            )]),
                                        );
                                    }
                                    let opts: Vec<SelectOption> = std::iter::once(
                                        SelectOption::new("provider default voice"),
                                    )
                                    .chain(d.voices.iter().map(|v| SelectOption::new(v.clone())))
                                    .collect();
                                    let voices_list = d.voices.clone();
                                    field(
                                        &t,
                                        "voice",
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
                                        .child(field(
                                            &t,
                                            "voice",
                                            line(vec![span(
                                                format!("voice catalog failed: {}", e.message),
                                                t.error,
                                            )]),
                                        ))
                                        .child(field(
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
                                        .child(field(
                                            &t,
                                            "",
                                            line(vec![span(
                                                "(or type {\"voice\": \"…\"} into options below)",
                                                t.text_faint,
                                            )]),
                                        ))
                                        .build()
                                }
                                _ => field(
                                    &t,
                                    "voice",
                                    line(vec![span("⟳ loading voices…", t.info)]),
                                ),
                            }
                        }))
                        .child(field(
                            &t,
                            "base URL",
                            TextInput::new()
                                .value(base_url)
                                .placeholder("optional — endpoint override")
                                .layout(LayoutStyle::default().w(52).h(1))
                                .element(gcx, &t)
                                .build(),
                        ))
                        // Reasoning belongs to TEXT GENERATION only —
                        // the row is absent (not disabled) on every
                        // other route, exactly as the web console hides
                        // it. `not set` clears the stored effort.
                        .child(if is_text {
                            field(
                                &t,
                                "reasoning",
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
                        .child(field(
                            &t,
                            "options (JSON)",
                            TextInput::new()
                                .value(options_json)
                                .placeholder("optional — e.g. {\"voice\": \"M3\"}")
                                .layout(LayoutStyle::default().w(52).h(1))
                                .element(gcx, &t)
                                .build(),
                        ))
                        .child(if is_text {
                            field(
                                &t,
                                "MTP default",
                                Select::new(["inherit", "off", "depth 2", "depth 3", "depth 4", "depth 5", "custom (JSON)"]
                                    .iter().map(|label| SelectOption::new(*label)).collect::<Vec<_>>())
                                    .value(speculation_ix)
                                    .on_change(move |ix| {
                                        match speculation_options(&options_json.get_untracked(), ix) {
                                            Ok(value) => { options_json.set(value); form_error.set(None); }
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
                LayoutStyle::default().h(1).shrink(0.0),
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
                    let save_enabled = overriding && provider_ok && model_ok && !busy_form;
                    let test_enabled = overriding && provider_ok && model_ok && !testing;
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
                    Element::new()
                        .style(LayoutStyle::row().gap(2))
                        .child(
                            Button::new("Save override")
                                .disabled(!save_enabled)
                                .on_click(move || {
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
                                    let opts_text = options_json.get_untracked();
                                    let opts_text = opts_text.trim();
                                    let options: Option<Value> = if opts_text.is_empty() {
                                        None
                                    } else {
                                        match serde_json::from_str::<Value>(opts_text) {
                                            Ok(v) if v.is_object() => Some(v),
                                            Ok(_) => {
                                                form_error.set(Some(
                                                    "options must be a JSON object".into(),
                                                ));
                                                return;
                                            }
                                            Err(e) => {
                                                form_error.set(Some(format!(
                                                    "options JSON does not parse: {e}"
                                                )));
                                                return;
                                            }
                                        }
                                    };
                                    // THE SAVE SENDS WHAT THIS FORM
                                    // OWNS, AND NOTHING ELSE — and it
                                    // sends every owned field
                                    // EXPLICITLY, "" included. The
                                    // write path preserves fields it is
                                    // not given (core_config.py's
                                    // field-preserving merge), so an
                                    // OMITTED empty base URL / options
                                    // would silently restore the stored
                                    // value the operator just cleared.
                                    // Fields with no control here
                                    // (reasoning on a non-text route)
                                    // stay unnamed on purpose.
                                    let mut body = json!({
                                        "provider": provider,
                                        "model": model,
                                        "base_url": base_url.get_untracked().trim(),
                                        "options": options.unwrap_or_else(|| json!({})),
                                    });
                                    if is_text {
                                        body["reasoning"] = Value::String(
                                            crate::store::REASONING_LEVELS
                                                .get(
                                                    reasoning_ix
                                                        .get_untracked()
                                                        .wrapping_sub(1),
                                                )
                                                .map(|s| (*s).to_string())
                                                .unwrap_or_default(),
                                        );
                                    }
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
                                })
                                .element(gcx, &t)
                                .build(),
                        )
                        .child(
                            // Test auditions the CURRENT UNSAVED picks
                            // through the production lane — proving the
                            // selection before it is stored (web parity).
                            Button::new("Test")
                                .disabled(!test_enabled)
                                .on_click(move || {
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
                                        let text = options_json.get_untracked();
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
                                })
                                .element(gcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Clear override")
                                .disabled(!clear_enabled)
                                .on_click(move || {
                                    // ONE policy for a destructive clear:
                                    // the same danger confirm as the
                                    // table's x. Close this editor first
                                    // (prompt-over-modal would stack two
                                    // modals — the engine hazard), then
                                    // prompt on the SCREEN scope, which
                                    // outlives the editor.
                                    close_after_clear();
                                    confirm_clear(screen_cx, &ctx_c, row4.clone());
                                })
                                .element(gcx, &t)
                                .build(),
                        )
                        .child(
                            Button::new("Cancel (Esc)")
                                .on_click(move || close_b())
                                .element(gcx, &t)
                                .build(),
                        )
                        .build()
                },
            ))
            .build()
    });
}

#[cfg(test)]
mod speculation_tests {
    use super::*;

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
}
