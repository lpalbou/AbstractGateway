//! Review & Test: the INLINE sandbox workspace (prove a provider/model
//! pair with a real generation), the change journal (every write + its
//! GET verification), and the wizard finish.
//!
//! Redesign 2026-07-25 (operator: "so much empty space and yet using a
//! modal… warrants a complete redesign"): the sandbox left its modal
//! and became the screen's body — pickers, a multiline prompt and the
//! FULL response now live in the space the screen used to waste, the
//! web console's Sandbox-tab-as-workspace shape. The journal keeps its
//! verify-after-write honesty in a compact bottom block that sizes to
//! its content (empty = 3 rows, capped + scrolling when long); the
//! outcome slot persists across navigation and always NAMES the pair
//! it belongs to, so a stale-attribution lie is impossible.
//!
//! Picker state is stored BY NAME in durable UiState signals
//! (`sb_provider` / `sb_model`) — indices are per-list and die with a
//! reload; names survive tab switches and carry the Providers-screen
//! `t` prefill. The fabricated-selection law holds throughout:
//! placeholder-first pickers, provider-switch resets the model, and a
//! saved name missing from a fresh list resolves to the placeholder
//! (never a silently different row).

use super::util::{error_panel_hint, field, line, span, span_bold, wrap_text};
use super::widths;
use super::Ctx;
use crate::store::{ConnPhase, Loadable, SandboxOutcome};
use crate::worker::Cmd;
use abstracttui::prelude::*;
use abstracttui::widgets::{TextArea, TextAreaState};

/// The shared sandbox entry (Providers `t`, and any future screen's
/// "test this" verb): land on the Review & Test screen with the
/// provider pre-pinned. The sandbox is INLINE there — one
/// implementation of the pair-test surface, one honest result slot —
/// so this navigates instead of opening a duplicate modal picker.
pub fn open_sandbox(ctx: &Ctx, provider: Option<String>) {
    if let Some(p) = provider {
        if ctx.ui.sb_provider.get_untracked() != p {
            ctx.ui.sb_provider.set(p);
            // Provider changed → the model pick belongs to the OLD
            // provider (fabricated-selection law: reset, never carry).
            ctx.ui.sb_model.set(String::new());
            ctx.ui.sb_model_custom.set(String::new());
        }
        // No notice here: the screen-switch effect retires notices on
        // arrival (they are screen-scoped) — the pinned provider in the
        // picker IS the visible acknowledgment.
    }
    ctx.ui.screen.set(super::SCREEN_REVIEW);
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    // Picker indices are page-scoped (they index the CURRENT lists);
    // the durable truth is the name signals in UiState. PageHost
    // remounts the page on activation, so a `t` prefill from the
    // Providers screen lands here through the derive effects below.
    let prov_ix = cx.signal(0usize);
    let model_ix = cx.signal(0usize);

    // Re-derive the provider index whenever discovery lands OR the
    // durable name changes (BOTH tracked — pages build eagerly at app
    // mount, so an untracked name read runs once with "" and a later
    // Providers-`t` prefill never lands; the equality guards below
    // keep the write-in-effect from looping). A saved name missing
    // from a fresh non-empty list clears (a gateway switch must never
    // show gateway A's provider under gateway B's header); while the
    // list is absent the name is kept and resolves when discovery
    // answers.
    cx.effect(move || {
        let names = provider_names_tracked(&store);
        let want = ui.sb_provider.get();
        if want.is_empty() {
            if prov_ix.get_untracked() != 0 {
                prov_ix.set(0);
            }
            return;
        }
        match names.iter().position(|n| *n == want) {
            Some(i) => {
                if prov_ix.get_untracked() != i + 1 {
                    prov_ix.set(i + 1);
                }
            }
            None if !names.is_empty() => {
                prov_ix.set(0);
                ui.sb_provider.set(String::new());
            }
            None => {}
        }
    });

    // Same derivation for the model pick against its provider's list.
    // Only a READY non-empty list can judge a saved name; Loading /
    // Failed lists keep the name for when discovery answers.
    cx.effect(move || {
        let ix = prov_ix.get();
        if ix == 0 {
            if model_ix.get_untracked() != 0 {
                model_ix.set(0);
            }
            return;
        }
        let names = provider_names_tracked(&store);
        let Some(name) = names.get(ix - 1).cloned() else {
            return;
        };
        let list = store
            .models
            .with(|m| m.get(&name).and_then(|l| l.ready().cloned()));
        // Tracked for the same eager-build reason as the provider
        // effect: a prefill written after mount must still derive.
        let want = ui.sb_model.get();
        match list {
            Some(models) if !models.is_empty() => match models.iter().position(|m| *m == want) {
                Some(i) => {
                    if model_ix.get_untracked() != i + 1 {
                        model_ix.set(i + 1);
                    }
                }
                None => {
                    if model_ix.get_untracked() != 0 {
                        model_ix.set(0);
                    }
                    if !want.is_empty() {
                        ui.sb_model.set(String::new());
                    }
                }
            },
            _ => {}
        }
    });

    // Load models for the picked provider (the route-editor contract:
    // untracked needs-check so a Failed entry cannot loop; the explicit
    // Retry button and a fresh pick are the retry paths).
    {
        let ctx2 = ctx.clone();
        cx.effect(move || {
            let ix = prov_ix.get();
            if ix == 0 {
                return;
            }
            let names = provider_names_tracked(&store);
            let Some(name) = names.get(ix - 1).cloned() else {
                return;
            };
            let needs = store.models.with_untracked(|m| !m.contains_key(&name));
            if needs {
                store
                    .models
                    .update(|m| drop(m.insert(name.clone(), Loadable::Loading)));
                ctx2.send(Cmd::LoadModels { provider: name });
            }
        });
    }

    // The prompt: a REAL multiline composer (the modal's single-line
    // TextInput undersold a prompt). State bridges to the durable
    // ui.sb_prompt so the draft survives tab switches and remounts.
    let prompt_state = TextAreaState::new(cx);
    prompt_state.set_text(ui.sb_prompt.get_untracked());

    let ctx_g = ctx.clone();
    let ctx_submit = ctx.clone();
    let ctx_btn = ctx.clone();
    let ctx_finish = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('g')), move |_| {
            run_sandbox_test(&ctx_g, prov_ix, model_ix);
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Live test (sandbox generate)")
                .fill(t.surface)
                // The workspace: grows into everything the journal
                // doesn't need. Fixed rows inside are pinned shrink(0)
                // (findings 1020/1030 — chrome pinned, content yields);
                // the RESPONSE region is the one that grows/shrinks.
                // Vertical padding dropped: at 80x24 those two rows are
                // the difference between fitting whole and fusing the
                // status line into the border (harness-caught).
                .layout(LayoutStyle::column().gap(0).grow(1.0).padding(Edges::hv(1, 0)))
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    line(vec![span(
                        "run a REAL text generation through the gateway to prove a provider/model pair works",
                        tt.text_muted,
                    )])
                }))
                // Provider picker — placeholder-first, honest states.
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
                    let ctx2 = ctx.clone();
                    move |gcx| {
                        let t = tt;
                        match store.providers.get() {
                            // NotAsked ≠ Loading (honest-states law):
                            // disconnected screens must not claim work
                            // is in flight.
                            Loadable::NotAsked => field(
                                &t,
                                "provider",
                                line(vec![span(
                                    "— not loaded yet (connect first, or press r to refresh)",
                                    t.text_muted,
                                )]),
                            ),
                            Loadable::Loading => field(
                                &t,
                                "provider",
                                line(vec![span("⟳ discovering providers…", t.info)]),
                            ),
                            Loadable::Failed(e) => {
                                let ctx3 = ctx2.clone();
                                Element::new()
                                    .style(LayoutStyle::column().gap(0))
                                    .child(field(
                                        &t,
                                        "provider",
                                        line(vec![span(
                                            format!("✗ discovery failed: {}", e.message),
                                            t.error,
                                        )]),
                                    ))
                                    .child(field(
                                        &t,
                                        "",
                                        Button::new("Retry provider discovery")
                                            .on_click(move || {
                                                ctx3.store.providers.set(Loadable::Loading);
                                                ctx3.send(Cmd::LoadProviders);
                                            })
                                            .element(gcx, &t)
                                            .build(),
                                    ))
                                    .build()
                            }
                            Loadable::Ready(d) if d.items.is_empty() => field(
                                &t,
                                "provider",
                                line(vec![span(
                                    "∅ no providers discovered — add one on the Providers screen (2)",
                                    t.text_muted,
                                )]),
                            ),
                            Loadable::Ready(d) => {
                                let names: Vec<String> =
                                    d.items.iter().map(|i| i.name.clone()).collect();
                                let opts: Vec<SelectOption> =
                                    std::iter::once(SelectOption::new("choose a provider…"))
                                        .chain(names.iter().map(|n| SelectOption::new(n.clone())))
                                        .collect();
                                field(
                                    &t,
                                    "provider",
                                    Select::new(opts)
                                        .value(prov_ix)
                                        .on_change(move |ix: usize| {
                                            let name = if ix == 0 || ix > names.len() {
                                                String::new()
                                            } else {
                                                names[ix - 1].clone()
                                            };
                                            if ui.sb_provider.get_untracked() != name {
                                                ui.sb_provider.set(name);
                                                // Provider switch resets the
                                                // model (fabricated-selection).
                                                ui.sb_model.set(String::new());
                                                ui.sb_model_custom.set(String::new());
                                                model_ix.set(0);
                                            }
                                        })
                                        .layout(LayoutStyle::default().w(40).h(1).shrink(0.0))
                                        .element(gcx, &t)
                                        .build(),
                                )
                            }
                        }
                    }
                }))
                // Model picker — mirrors the route editor's honesty
                // arms: list / loading / FAILED-with-retry / none.
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
                    let ctx2 = ctx.clone();
                    move |gcx| {
                        let t = tt;
                        let ix = prov_ix.get();
                        if ix == 0 {
                            return field(
                                &t,
                                "model",
                                line(vec![span("choose a provider first", t.text_faint)]),
                            );
                        }
                        let names = provider_names_tracked(&store);
                        let Some(name) = names.get(ix - 1).cloned() else {
                            return field(
                                &t,
                                "model",
                                line(vec![span("choose a provider first", t.text_faint)]),
                            );
                        };
                        let entry = store
                            .models
                            .with(|m| m.get(&name).cloned())
                            .unwrap_or(Loadable::NotAsked);
                        match entry {
                            Loadable::Ready(models) if !models.is_empty() => {
                                let opts: Vec<SelectOption> =
                                    std::iter::once(SelectOption::new("choose a model…"))
                                        .chain(models.iter().map(|m| SelectOption::new(m.clone())))
                                        .collect();
                                let models2 = models.clone();
                                field(
                                    &t,
                                    "model",
                                    Combobox::new(opts)
                                        .value(model_ix)
                                        .placeholder("type to filter…")
                                        .on_change(move |mix: usize| {
                                            let m = if mix == 0 || mix > models2.len() {
                                                String::new()
                                            } else {
                                                models2[mix - 1].clone()
                                            };
                                            if ui.sb_model.get_untracked() != m {
                                                ui.sb_model.set(m);
                                            }
                                        })
                                        .layout(LayoutStyle::default().w(52).h(1).shrink(0.0))
                                        .element(gcx, &t)
                                        .build(),
                                )
                            }
                            Loadable::Loading | Loadable::NotAsked => field(
                                &t,
                                "model",
                                line(vec![span("⟳ discovering models…", t.info)]),
                            ),
                            // Discovery FAILED ≠ "no models" — the same
                            // honesty + retry contract as the route editor.
                            Loadable::Failed(e) => {
                                let name_btn = name.clone();
                                let ctx3 = ctx2.clone();
                                Element::new()
                                    .style(LayoutStyle::column().gap(0))
                                    .child(field(
                                        &t,
                                        "model",
                                        TextInput::new()
                                            .value(ui.sb_model_custom)
                                            .placeholder("discovery failed — type the model id")
                                            .placeholder_while_focused(true)
                                            .layout(LayoutStyle::default().w(52).h(1))
                                            .element(gcx, &t)
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
                                                ctx3.store.models.update(|m| {
                                                    drop(m.insert(n.clone(), Loadable::Loading))
                                                });
                                                ctx3.send(Cmd::LoadModels { provider: n });
                                            })
                                            .element(gcx, &t)
                                            .build(),
                                    ))
                                    .build()
                            }
                            Loadable::Ready(_) => field(
                                &t,
                                "model",
                                TextInput::new()
                                    .value(ui.sb_model_custom)
                                    .placeholder("no discoverable models — type the model id")
                                    .placeholder_while_focused(true)
                                    .layout(LayoutStyle::default().w(52).h(1))
                                    .element(gcx, &t)
                                    .build(),
                            ),
                        }
                    }
                }))
                // The prompt composer: grows 1→4 rows with content;
                // Enter runs the test (Ctrl+J / Alt+Enter insert a
                // newline — the engine's universal-chord default).
                .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), {
                    let prompt_state = prompt_state.clone();
                    move |gcx| {
                        let t = tt;
                        let ctx3 = ctx_submit.clone();
                        let st = prompt_state.clone();
                        field(
                            &t,
                            "prompt",
                            TextArea::new()
                                .state(&st)
                                .placeholder("what to send — Enter runs the test")
                                .on_change(move |s: &str| {
                                    if ui.sb_prompt.with_untracked(|p| p != s) {
                                        ui.sb_prompt.set(s.to_string());
                                    }
                                })
                                .on_submit(move |_| {
                                    run_sandbox_test(&ctx3, prov_ix, model_ix);
                                })
                                // Layout override REPLACES the widget's
                                // grow-to-content band (its doc contract),
                                // so the band is hand-managed here: basis
                                // 0 + grow fills exactly the row's free
                                // width (auto basis inherits the widget's
                                // Percent(1.0) inner width and overpaints
                                // the block border — harness-caught); a
                                // fixed 2-row window scrolls internally
                                // beyond (Ctrl+J inserts newlines);
                                // shrink(0) keeps the composer
                                // uncrushable (0240 #2).
                                .layout(
                                    LayoutStyle::default()
                                        .basis(Dimension::Cells(0))
                                        .grow(1.0)
                                        .min_h(2)
                                        .max_h(2)
                                        .shrink(0.0),
                                )
                                .element(gcx, &t)
                                // The screen's focus anchor: ALWAYS
                                // mounted (picker arms come and go), so
                                // keys are never dead on a disconnected
                                // /empty screen (F2/F3 — harness-caught:
                                // with no focus, dispatch stops at root
                                // and the screen's g shortcut never
                                // fires). Enter here runs the test; Tab
                                // reaches the pickers — the "pick a
                                // provider first" refusal teaches that.
                                .autofocus()
                                .build(),
                        )
                    }
                }))
                // (attachments land here: a chips row + Ctrl+A picker —
                // abstracttui 0.2.20 on_paste/FilePicker; see
                // abstracttui/reviews/console-tui-attachments-integration-prompt.md.
                // The slot is this column position, under the prompt.)
                .child(dyn_view_scoped(LayoutStyle::default().h(1).shrink(0.0), {
                    move |gcx| {
                        let t = tt;
                        let ctx3 = ctx_btn.clone();
                        let running = store.sandbox.with(Loadable::is_loading);
                        Element::new()
                            .style(LayoutStyle::row().gap(2))
                            .child(
                                // Enter is the advertised gesture (the
                                // prompt holds focus, so a bare g TYPES
                                // there); g still fires from picker/
                                // button focus — kept for the old
                                // teaching, no longer primary.
                                Button::new("Generate (Enter)")
                                    .disabled(running)
                                    .on_click(move || {
                                        run_sandbox_test(&ctx3, prov_ix, model_ix);
                                    })
                                    .element(gcx, &t)
                                    .build(),
                            )
                            .build()
                    }
                }))
                // Result status: PINNED — the outcome the operator paid
                // tokens for must survive height pressure (the D1
                // fusion class). The response BODY below is the
                // yielding region.
                .child(dyn_view(LayoutStyle::default().shrink(0.0), move || {
                    sandbox_status(&tt, &store.sandbox.get())
                }))
                // Response body: the FULL text, scrolling — the empty
                // space became the workspace. (The old modal ellipsized
                // the paid-for response at 90 chars.) Model replies are
                // MARKDOWN (tui wave13 console-md-adoption): MarkdownView
                // typesets headings/tables/fences (json/yaml tint, table
                // crush degrades to a record list) with an intrinsic
                // measure, so Scroll works out of the box. ERROR text
                // stays plain wrapped spans — an error string is not a
                // document, and markdown-parsing it could restyle its
                // payload (e.g. a traceback with # lines as headings).
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), move |gcx| {
                    let t = tt;
                    let width = abstracttui::app::use_viewport(gcx).get().w;
                    // Block borders (2) + padding (2) + a safety cell.
                    let wrap_w = (width as usize).saturating_sub(6).max(20);
                    let (text, is_reply) = match &store.sandbox.get() {
                        Loadable::Ready(o) if o.ok => (o.response.trim().to_string(), true),
                        Loadable::Ready(o) => (
                            o.error
                                .clone()
                                .unwrap_or_else(|| "no error detail in the payload".into()),
                            false,
                        ),
                        _ => (String::new(), false),
                    };
                    if text.is_empty() {
                        return Element::new().style(LayoutStyle::default().h(0)).build();
                    }
                    if is_reply {
                        return Scroll::new(MarkdownView::new(text).view(gcx))
                            .scrollbar_auto_hide(true)
                            .view(gcx);
                    }
                    let rows: Vec<View> = wrap_text(&text, wrap_w)
                        .into_iter()
                        .map(|l| line(vec![span(l, t.text)]))
                        .collect();
                    Scroll::new(
                        Element::new().style(LayoutStyle::column()).children(rows).build(),
                    )
                    // A track over a fitting response reads as clutter;
                    // the bar appears exactly when there is more to see.
                    .scrollbar_auto_hide(true)
                    .view(gcx)
                }))
                .element(t)
                .build(),
        )
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title("Changes this session (apply → verify via GET)")
                .fill(t.surface)
                // Compact receipt strip: sizes to CONTENT (empty = one
                // line; 2 rows per entry), capped height-aware, then
                // scrolls — an empty journal no longer owns half the
                // screen (the operator complaint), a long one never
                // evicts the sandbox, and a tall terminal shows more.
                // The Scroll's appetite must never drive the block's
                // auto height (harness-caught: an uncapped Scroll child
                // ballooned the block to its max and starved the
                // sandbox into border fusion) — hence the explicit
                // h(content.min(cap)) wrapper below.
                .layout(LayoutStyle::column().gap(0).shrink(0.0).padding(Edges::hv(1, 0)))
                .child(dyn_view_scoped(
                    LayoutStyle::default().shrink(0.0),
                    move |gcx| {
                        let jw = abstracttui::app::use_viewport(gcx).get().w
                            - widths::BLOCK_CHROME;
                        let entries = store.journal.get();
                        if entries.is_empty() {
                            return line(vec![span(
                                "no changes applied this session — the wizard only writes when you save",
                                tt.text_muted,
                            )]);
                        }
                        let mut rows: Vec<View> = Vec::new();
                        for e in entries.iter().rev() {
                            let (mark, ink) = match &e.outcome {
                                Ok(_) => ("✓", tt.ok),
                                Err(_) => ("✗", tt.error),
                            };
                            rows.push(line(vec![
                                span(format!("{} {} ", e.when, mark), ink),
                                span_bold(e.action.clone(), tt.text),
                                match &e.outcome {
                                    Ok(o) => span(format!("  {o}"), tt.text_muted),
                                    // Budgeted against the row, not capped
                                    // at a constant: the failure reason is
                                    // the reason the operator is reading.
                                    Err(err) => span(
                                        format!(
                                            "  {}",
                                            widths::head_fit(
                                                err,
                                                widths::elastic_budget(
                                                    &[
                                                        &format!("{} {} ", e.when, mark),
                                                        &e.action,
                                                        "  ",
                                                    ],
                                                    jw,
                                                ),
                                            )
                                        ),
                                        tt.error,
                                    ),
                                },
                            ]));
                            match &e.verified {
                                Some(Ok(v)) => rows.push(line(vec![span(
                                    format!("        verified · {v}"),
                                    tt.ok,
                                )])),
                                Some(Err(v)) => rows.push(line(vec![span_bold(
                                    format!("        VERIFY FAILED · {v}"),
                                    tt.error,
                                )])),
                                None => rows.push(line(vec![span(
                                    "        verify GET unavailable".to_string(),
                                    tt.warn,
                                )])),
                            }
                        }
                        // Tall terminals see more receipts; tight ones
                        // keep the sandbox whole (cap 3 is what makes
                        // the worst pinned state — wizard + Failed
                        // outcome — land exactly inside 80x24; the
                        // 4-state harness matrix proves the arithmetic).
                        // Newest entries render first, so the cap hides
                        // only the oldest; the scroll reaches them.
                        let cap = if abstracttui::app::use_viewport(gcx).get().h >= 30 {
                            10
                        } else {
                            3
                        };
                        let h = (rows.len() as i32).min(cap);
                        // Returned DIRECTLY (no wrapper): the dyn slot
                        // is row-direction, so the Scroll's own
                        // grow(1.0)+basis(0) is what fills the width —
                        // an interposed column wrapper leaves draw-lines
                        // at their 0 intrinsic width (blank journal,
                        // bar only; harness-caught).
                        Scroll::new(
                            Element::new()
                                .style(LayoutStyle::column())
                                .children(rows)
                                .build(),
                        )
                        .layout(
                            LayoutStyle::default()
                                .grow(1.0)
                                .basis(Dimension::Cells(0))
                                .h(h),
                        )
                        // Bar only when entries exceed the cap.
                        .scrollbar_auto_hide(true)
                        .view(gcx)
                    },
                ))
                .element(t)
                .build(),
        )
        .child(dyn_view_scoped(LayoutStyle::default().shrink(0.0), move |gcx| {
            let t = tt;
            if !ui.wizard.get() {
                return line(vec![span(
                    "browse mode — changes apply immediately as you save them",
                    t.text_faint,
                )]);
            }
            let ctx3 = ctx_finish.clone();
            Element::new()
                .style(LayoutStyle::row().gap(2).h(1))
                .child(
                    Button::new("Finish — switch to browse mode")
                        .on_click(move || {
                            ctx3.ui.wizard.set(false);
                            ctx3.store.notice.set(Some(
                                "wizard finished — browse with 1-8, q quits".into(),
                            ));
                        })
                        .element(gcx, &t)
                        .build(),
                )
                .build()
        }))
        .build()
}

/// Tracked provider-name read (the picker's reactive source; the
/// untracked twin lives in providers::provider_names for modal-open
/// snapshots).
fn provider_names_tracked(store: &crate::store::Store) -> Vec<String> {
    store.providers.with(|p| {
        p.ready()
            .map(|d| d.items.iter().map(|i| i.name.clone()).collect())
            .unwrap_or_default()
    })
}

/// The one Generate path (g key, Enter in the prompt, the button).
/// Every refusal NAMES its reason (F2/F3 — a silent key reads as a
/// dead app); the synchronous Loading write is the double-press guard
/// (the worker's own Loading post arrives a beat later — a rapid
/// double-press would fire TWO real generations without it).
fn run_sandbox_test(ctx: &Ctx, prov_ix: Signal<usize>, model_ix: Signal<usize>) {
    let store = ctx.store;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        store.notice.set(Some(
            "connect to the gateway first — the sandbox runs real generations".into(),
        ));
        return;
    }
    let names = crate::ui::providers::provider_names(&store);
    let ix = prov_ix.get_untracked();
    if ix == 0 || ix > names.len() {
        store.notice.set(Some(
            "pick a provider first — Tab reaches the picker".into(),
        ));
        return;
    }
    let name = names[ix - 1].clone();
    let list = store
        .models
        .with_untracked(|m| m.get(&name).and_then(|l| l.ready().cloned()));
    let model = match list {
        Some(models) if !models.is_empty() => {
            let mix = model_ix.get_untracked();
            if mix == 0 || mix > models.len() {
                store
                    .notice
                    .set(Some("pick a model — the list is loaded".into()));
                return;
            }
            models[mix - 1].clone()
        }
        _ => {
            let custom = ctx.ui.sb_model_custom.get_untracked().trim().to_string();
            if custom.is_empty() {
                store
                    .notice
                    .set(Some("pick or type a model id before generating".into()));
                return;
            }
            custom
        }
    };
    let prompt = ctx.ui.sb_prompt.get_untracked();
    if prompt.trim().is_empty() {
        store.notice.set(Some(
            "type a prompt — the test sends it to the model".into(),
        ));
        return;
    }
    if store.sandbox.with_untracked(Loadable::is_loading) {
        store
            .notice
            .set(Some("a test is already running — one at a time".into()));
        return;
    }
    store.sandbox.set(Loadable::Loading);
    ctx.send(Cmd::SandboxTest {
        provider: name,
        model,
        prompt,
    });
}

/// The pinned outcome header: state + the PAIR it belongs to (the
/// persistent slot survives navigation, so the header must always name
/// its subject — the pickers may point elsewhere by now).
fn sandbox_status(t: &TokenSet, s: &Loadable<SandboxOutcome>) -> View {
    match s {
        Loadable::NotAsked => line(vec![span(
            "no test run yet — pick a provider and model, then Generate",
            t.text_faint,
        )]),
        Loadable::Loading => line(vec![span(
            "⟳ generating… (a real model call — can take tens of seconds)",
            t.info,
        )]),
        Loadable::Failed(e) => error_panel_hint(t, e, Some("press g / Generate to retry")),
        Loadable::Ready(o) => {
            if o.ok {
                let routed = match (&o.routed_provider, &o.profile) {
                    (Some(rp), Some(pf)) => format!("routed via {rp} (profile {pf})"),
                    (Some(rp), None) => format!("routed via {rp}"),
                    _ => "routing not reported".to_string(),
                };
                Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![
                        span_bold("✓ ", t.ok),
                        span_bold(format!("{} / {}", o.provider, o.model), t.text),
                        span(format!("  {routed}"), t.text_muted),
                    ]))
                    .child(line(vec![span(
                        format!("  usage: {}", o.usage.clone().unwrap_or_else(|| "—".into())),
                        t.text_faint,
                    )]))
                    .build()
            } else {
                line(vec![span_bold(
                    format!("✗ {} / {} — gateway reports ok:false", o.provider, o.model),
                    t.error,
                )])
            }
        }
    }
}
