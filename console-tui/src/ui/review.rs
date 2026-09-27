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
//!
//! 2026-09-27: the sandbox workspace (every web output mode — text,
//! image, voice, music, SFX, video) lives in `ui/sandbox.rs`; this
//! screen mounts it above the journal and the wizard finish.

use super::util::{line, span, span_bold};
use super::widths;
use super::Ctx;
use abstracttui::prelude::*;

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
    let ctx_finish = ctx.clone();

    Element::new()
        .style(LayoutStyle::column().gap(0))
        // The sandbox workspace (ui/sandbox.rs): grows into everything
        // the journal doesn't need; owns the `g` run key.
        .child(super::sandbox::workspace(cx, ctx, t))
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
                                Ok(_) if e.attention.is_some() => ("!", tt.warn),
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
                            if let Some(a) = &e.attention {
                                rows.push(line(vec![span_bold(
                                    format!("        needs attention · {a}"),
                                    tt.warn,
                                )]));
                            }
                            match &e.verified {
                                Some(Ok(v)) => rows.push(line(vec![span(
                                    format!("        verified · {v}"),
                                    if e.attention.is_some() { tt.text_muted } else { tt.ok },
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
            // The guide's last step: Finish / Skip setup record the
            // first-run outcome on the gateway (ui/welcome.rs).
            super::welcome::finish_row(gcx, &ctx_finish, &t)
        }))
        .build()
}
