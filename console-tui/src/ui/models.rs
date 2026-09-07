//! Models: the "agentic OS" resources view — what is resident on the
//! execution host right now (RAM / device / GPU gauges, the resident
//! model table, session prompt caches) plus the operator verbs over it
//! (unload, lock/unlock, warm up, context estimate, clear caches).
//!
//! Data is ONE slow snapshot (`GET /host/state` — GPU probe + residency
//! listing) polled ~4s apart WHILE THIS TAB IS ON SCREEN and never
//! faster (the poll chain lives in ui/mod.rs + the worker's
//! `PollHostState` arm, generation-gated so it dies on tab exit).
//! Every section of the snapshot is independently best-effort;
//! `degraded`/`reasons` render as muted notes, never blank-success.

use abstracttui::base::Rgba;
use abstracttui::prelude::*;
use abstracttui::widgets::{Progress, Table, Tabs, Tone};

use super::util::{badge, field, field_w, line, loadable_view, span, span_bold, wrap_text};
use super::widths;
use super::widths::BLOCK_CHROME;
use super::Ctx;
use crate::store::{
    human_bytes, lock_action, memory_breakdown, resident_label, size_marked, unload_refusal,
    BreakdownKind, HostStateData, Loadable, LockAction, ModelRow, SessionCacheRow,
    ACCELERATOR_NOTE,
};
use crate::worker::Cmd;

// ---------------------------------------------------------------------
// Pure row/label builders (unit-tested below — the cache_rows precedent)
// ---------------------------------------------------------------------

/// Task → (badge tone, canonical label). The labels are the gateway's
/// OWN vocabulary (`contracts.common.model_residency.modality_ui` —
/// canonical for every residency client); the hex colors that ride with
/// them are for web clients, so a token-themed TUI maps each label
/// family onto ONE stable abstracttui Badge tone instead:
///
/// | family                                    | label     | tone   |
/// |-------------------------------------------|-----------|--------|
/// | text_generation                           | Text      | Accent |
/// | image_generation / image_to_image /       | Image     | Ok     |
/// |   image_upscale                           |           |        |
/// | video_generation / text_to_video /        | Video     | Info   |
/// |   image_to_video                          |           |        |
/// | tts / stt                                 | Voice     | Info   |
/// | music_generation                          | Music     | Warn   |
/// | scene3d_generation / text_to_scene3d /    | 3D        | Muted  |
/// |   image_to_scene3d                        |           |        |
/// | embedding                                 | Embedding | Muted  |
/// | null / anything else                      | ?         | Muted  |
///
/// Six tones cannot carry eight families distinctly — the LABEL is the
/// discriminator, the tone is the family accent (Video and Voice share
/// Info; 3D and Embedding share Muted). A null task renders the muted
/// "?" — unknown, never guessed.
pub fn task_tone(task: Option<&str>) -> (Tone, &'static str) {
    match task {
        Some("text_generation") => (Tone::Accent, "Text"),
        Some("image_generation") | Some("image_to_image") | Some("image_upscale") => {
            (Tone::Ok, "Image")
        }
        Some("video_generation") | Some("text_to_video") | Some("image_to_video") => {
            (Tone::Info, "Video")
        }
        Some("tts") | Some("stt") => (Tone::Info, "Voice"),
        Some("music_generation") => (Tone::Warn, "Music"),
        Some("scene3d_generation") | Some("text_to_scene3d") | Some("image_to_scene3d") => {
            (Tone::Muted, "3D")
        }
        Some("embedding") => (Tone::Muted, "Embedding"),
        _ => (Tone::Muted, "?"),
    }
}

/// The size cell — THE COALESCE (`size_bytes` → `size_vram_bytes` →
/// `est_weights_bytes`, [`ModelRow::display_size`]) with the estimate
/// MARKER: `3.1 GB` is a figure the host reported, `~3.1 GB` one it
/// estimated from the artifact. Honest dash when it reported neither.
/// (Before this rule a sweep row carrying only `est_weights_bytes` —
/// every externally-loaded LM Studio model — rendered BLANK.)
fn size_cell(r: &ModelRow) -> String {
    r.display_size()
        .map(|(b, est)| size_marked(b, est))
        .unwrap_or_else(|| "—".into())
}

/// The per-model KV cache — a SECOND figure beside the weights, never
/// folded into the size. Unknown renders the dash, never a 0.
fn cache_cell(r: &ModelRow) -> String {
    r.cache_bytes.map(human_bytes).unwrap_or_else(|| "—".into())
}

/// The context cell: `8192*` when the host CALIBRATED the value (a
/// measured fact), bare `8192` when merely configured, `—` unknown.
fn ctx_cell(r: &ModelRow) -> String {
    match r.context_length {
        Some(n) if r.context_calibrated == Some(true) => format!("{n}*"),
        Some(n) => n.to_string(),
        None => "—".into(),
    }
}

/// The lock cell. `⊘` (U+2298), deliberately NOT the padlock emoji:
/// the padlock is Emoji=Yes, measures 2 cells and terminals draw it at
/// their own advance, sliding every column to its right (this crate's
/// documented glyph law — see routes.rs). Blank = not locked, or lock
/// state unknown (a null must never render as locked).
fn lock_cell(r: &ModelRow) -> String {
    if r.locked == Some(true) {
        "⊘".into()
    } else {
        String::new()
    }
}

/// One model row's table cells — column order:
/// modality · provider · model · resident · size · cache · ctx · lock ·
/// default.
pub fn model_row_cells(r: &ModelRow) -> Vec<String> {
    let (_, label) = task_tone(r.task.as_deref());
    vec![
        label.to_string(),
        r.provider.clone().unwrap_or_else(|| "—".into()),
        r.model.clone().unwrap_or_else(|| "—".into()),
        resident_label(r.resident).to_string(),
        size_cell(r),
        cache_cell(r),
        ctx_cell(r),
        lock_cell(r),
        if r.default == Some(true) {
            "✓".into()
        } else {
            String::new()
        },
    ]
}

/// One session-cache row's table cells:
/// key · session · model · size · tokens.
pub fn cache_row_cells(r: &SessionCacheRow) -> Vec<String> {
    vec![
        r.key.clone(),
        if r.session_id.is_empty() {
            "—".into()
        } else {
            r.session_id.clone()
        },
        if r.model.is_empty() {
            "—".into()
        } else {
            r.model.clone()
        },
        r.bytes.map(human_bytes).unwrap_or_else(|| "—".into()),
        r.token_count
            .map(|n| n.to_string())
            .unwrap_or_else(|| "—".into()),
    ]
}

/// The footer totals line. Byte totals are the sum of KNOWN sizes —
/// when no row carried one the answer is "—", never a fabricated 0.
/// The model count names RESIDENT rows separately from the row total:
/// "N model(s)" alone would present configured/cached rows as loaded
/// (default ≠ loaded — the tri-state Resident column is the row truth).
pub fn totals_line(d: &HostStateData) -> String {
    let bytes = |b: Option<u64>| b.map(human_bytes).unwrap_or_else(|| "—".into());
    let resident = d.models.iter().filter(|m| m.is_resident()).count();
    format!(
        "totals: {} resident / {} model row(s) · {} · {} session cache(s) · {}",
        resident,
        d.models.len(),
        bytes(d.model_bytes),
        d.caches.len(),
        bytes(d.cache_bytes),
    )
}

// ---------------------------------------------------------------------
// The screen
// ---------------------------------------------------------------------

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;

    super::util::clamp_selection(cx, ui.model_sel, move || {
        store
            .host_state
            .with(|d| d.ready().map(|d| d.models.len()).unwrap_or(0))
    });
    super::util::clamp_selection(cx, ui.cache_sel, move || {
        store
            .host_state
            .with(|d| d.ready().map(|d| d.caches.len()).unwrap_or(0))
    });

    // The 409 model_locked second confirm: the worker fills
    // `store.unload_locked` when the gateway refuses an unload because
    // the model is pinned; this effect consumes the slot and offers
    // "Force unload?". PAGE scope — the prompt survives the 4s poll's
    // region rebuilds.
    {
        let ctx2 = ctx.clone();
        cx.effect(move || {
            let Some((p, m)) = store.unload_locked.get() else {
                return;
            };
            store.unload_locked.set(None);
            let ctx3 = ctx2.clone();
            let (p2, m2) = (p.clone(), m.clone());
            super::confirm_danger(
                cx,
                ctx2.ui,
                format!(
                    "{p}/{m} is locked — the gateway refused the unload. Force unload anyway?"
                ),
                "Force unload",
                "Keep it loaded",
                move || {
                    send_mutation(&ctx3, format!("force-unloading {p2}/{m2}"), |op| {
                        Cmd::UnloadModel {
                            provider: p2,
                            model: m2,
                            force: true,
                            op,
                        }
                    });
                },
            );
        });
    }

    let ctx_unload = ctx.clone();
    let ctx_lock = ctx.clone();
    let ctx_warm = ctx.clone();
    let ctx_est = ctx.clone();
    let ctx_clear = ctx.clone();
    let detail_top = ui.models_detail_top;
    // First-mount-only autofocus (the runtimes-table pattern): this
    // screen's table region regenerates on EVERY ~4s poll — a re-armed
    // autofocus would yank focus back mid-keystroke each time.
    let autofocus_armed = std::rc::Rc::new(std::cell::Cell::new(false));

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .shortcut(KeyChord::plain(Key::Char('u')), move |_| {
            unload_selected(cx, &ctx_unload);
        })
        .shortcut(KeyChord::plain(Key::Char('k')), move |_| {
            toggle_lock_selected(cx, &ctx_lock);
        })
        .shortcut(KeyChord::plain(Key::Char('w')), move |_| {
            open_warmup_form(cx, &ctx_warm);
        })
        .shortcut(KeyChord::plain(Key::Char('e')), move |_| {
            estimate_selected(&ctx_est);
        })
        .shortcut(KeyChord::plain(Key::Char('c')), move |_| {
            clear_caches_selected(cx, &ctx_clear);
        })
        // `m` pages the memory itemization. It is a plain char on purpose:
        // PageUp/PageDown reach the focused Table first (the engine runs
        // element handlers BEFORE shortcuts), and `[`/`]` are the root's
        // screen switches. The body reads this counter MODULO the live
        // window count, so a bare `+ 1` here is always a valid position and
        // wraps back to the top at the end.
        .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
            detail_top.update(|n| *n = n.saturating_add(1));
        })
        .child(
            Block::new()
                .border(BorderKind::Rounded)
                .title(
                    "Resources — what is resident on the execution host right now \
                     · ~ = estimated size, not measured",
                )
                .fill(t.surface)
                .layout(
                    LayoutStyle::column()
                        .gap(0)
                        .grow(1.0)
                        .padding(Edges::all(1))
                        // THE BOTTOM BORDER IS NOT A DRAWING SURFACE. Every
                        // `line(...)` here paints at its own `rect.y` and
                        // clips on X only, and flex hands an over-demanded
                        // `shrink(0.0)` child a y past this box — which is
                        // how the selected-row detail line came to sit ON the
                        // `╰────╯` run at 80x24, leaving the block with no
                        // closing corner. The budget in `body` is what stops
                        // the overflow; this is the guarantee that a future
                        // one cannot reach the frame.
                        .clip(),
                )
                .child(dyn_view_scoped(LayoutStyle::default().grow(1.0), {
                    let ctx_body = ctx.clone();
                    move |gcx| {
                        let data = store.host_state.get();
                        let _ = &ctx_body;
                        loadable_view(
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |_d: &HostStateData| false, // the strip renders even with zero rows
                            "",
                            |d| {
                                let first = !autofocus_armed.get();
                                if first {
                                    autofocus_armed.set(true);
                                }
                                body(gcx, &tt, ui, d, first)
                            },
                        )
                    }
                }))
                .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                    // The totals footer — pinned so the tables' growth
                    // never squeezes it out.
                    let text = store
                        .host_state
                        .with(|d| d.ready().map(totals_line))
                        .unwrap_or_default();
                    line(vec![span(text, tt.text_faint)])
                }))
                .element(t)
                .build(),
        )
        .build()
}

/// Rows the page chrome takes OUTSIDE this screen's body region: the
/// title row and its blank, the screen tab bar and its underline (4), the
/// footer's blank and hint row (2), the Block's own top and bottom border
/// (2), and the pinned totals footer inside it (1).
///
/// It is a constant because the engine hands a view its own rect only at
/// DRAW time and the budget below has to be decided during BUILD. The
/// 80x24 and 110x34 layout tests pin the outcome, so a chrome change fails
/// loudly instead of silently squeezing the Loaded table off screen again.
const PAGE_CHROME_ROWS: usize = 9;

/// The Ready body: the PINNED head (meters, the accelerator's scoped label
/// and its note, degradation notes), then the WINDOWED memory itemization,
/// then the Loaded/Caches sub-tabs and the selected row's detail line.
///
/// THE 80x24 RULE. Every one of those parts refuses to shrink, and the
/// itemization's natural height is ~11 rows on a real host — so it used to
/// take the whole block and push the sub-tabs, the table and the detail
/// line past the bottom border: at 80x24, 80x26 and 110x24 the Loaded
/// table had ZERO model rows and neither `Tab` nor ten `Down` presses
/// brought it back, while the detail line painted over the `╰────╯`. This
/// crate treats 80x24 as supported (`connection_screen_fits_at_macos_default_80x24`,
/// `runtimes_inspector_fits_at_80x24`), and on THIS screen 80x24 is where
/// the operator most needs the table: the lock/unlock verb is per row.
///
/// So when the terminal cannot hold both, the table's rows are reserved
/// FIRST and the itemization gets what is left — windowed, with an
/// affordance naming the lines that are off screen and the key that pages
/// to them. When it CAN hold both, nothing is reserved and the itemization
/// renders whole, exactly as before. Nothing is shrunk and nothing is
/// reordered: at 80x24 the itemization is one `m` away instead of
/// unreachable, and the table is on screen instead of past the border.
fn body(cx: Scope, t: &TokenSet, ui: super::UiState, d: &HostStateData, autofocus: bool) -> View {
    let tt = *t;
    let models = d.models.clone();
    let caches = d.caches.clone();
    let models_detail = d.models.clone();
    let caches_detail = d.caches.clone();
    // The strip wraps the GGUF note to the live viewport (block borders
    // + padding + a safety cell), the review-screen precedent: a note
    // the spec forbids truncating may not meet the terminal's edge.
    let viewport = abstracttui::app::use_viewport(cx).get();
    let wrap_w = (viewport.w as usize)
        .saturating_sub(BLOCK_CHROME as usize + 6)
        .max(24);

    let head = head_rows(t, d);
    let detail = detail_rows(t, d, wrap_w);

    // The reservation, in order of what the operator cannot do without.
    const TABS_BAR: usize = 2; // the Loaded / Caches control surface
    const ROW_DETAIL: usize = 1; // the selected row's `k …` line
    const TABLE_MIN: usize = 2; // header + one row — the least that is usable
    let total = detail.len();
    let body_h = (viewport.h.max(0) as usize)
        .saturating_sub(PAGE_CHROME_ROWS + if ui.wizard.get() { 1 } else { 0 });

    // The table's reservation is CONDITIONAL, and the condition is whether
    // the terminal can hold both. When it can, nothing is reserved and the
    // itemization renders whole — a tall terminal has room for the full
    // accounting and the operator asked for the accounting. When it cannot,
    // the table's rows come FIRST: at 80x24 this screen's reason to exist is
    // the per-row lock/unlock verb, and the operator needs the rows and the
    // size/cache columns more than the full itemization, which is one `m`
    // away either way.
    let table_floor = if head.len() + total + TABS_BAR + ROW_DETAIL + TABLE_MIN <= body_h {
        TABLE_MIN
    } else {
        // Header + up to four rows: enough to see a row, move between rows
        // and read the columns. More than four and the itemization starts
        // losing lines that would otherwise have fitted.
        1 + d.models.len().clamp(1, 4)
    };
    let detail_cap =
        body_h.saturating_sub(head.len() + TABS_BAR + ROW_DETAIL + table_floor);

    // The window. `detail_cap - 1` because the affordance owns a row of the
    // budget; below 2 rows there is nothing left to window and only the
    // affordance renders, which is still the honest answer — it names the
    // lines and the key that reaches them.
    let win = if detail_cap >= total {
        total
    } else {
        detail_cap.saturating_sub(1)
    };
    let positions = total - win + 1; // 1 when everything fits

    // ONE `shrink(0.0)` column for the whole strip, exactly as before: a
    // `line(1)` row carries the flex default `shrink: 1.0`, so as loose
    // children of the body these rows get squeezed to nothing and paint
    // over each other — the first build of this fix lost the RAM gauge and
    // both accelerator lines that way, which is the very reading it exists
    // to keep on screen. The strip refuses to shrink; the BUDGET above is
    // what keeps it from needing to.
    let mut strip = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    for row in head {
        strip = strip.child(row);
    }
    // The window position gets its OWN region. `models_detail_top` must not
    // be read out here: this body lives in the poll region, so a tracked
    // read would rebuild the whole subtree on every `m` — including the
    // Table, which loses focus when it is re-created (autofocus is
    // first-mount-only, by design, so the 4s poll cannot yank focus
    // mid-keystroke). Measured: with the read out here the FIRST `m`
    // worked and every one after it went nowhere.
    let detail_top = ui.models_detail_top;
    let faint = tt.text_faint;
    strip = strip.child(dyn_view(
        LayoutStyle::column().gap(0).shrink(0.0),
        move || {
            let top = if win == total {
                0
            } else {
                detail_top.get() % positions
            };
            let mut col = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
            for (text, ink) in detail.iter().skip(top).take(win) {
                col = col.child(line(vec![span(text.clone(), *ink)]));
            }
            if win < total && detail_cap > 0 {
                let above = top;
                let below = total - top - win;
                let mut bits: Vec<String> = Vec::new();
                if above > 0 {
                    bits.push(format!("↑ {above} more"));
                }
                if below > 0 {
                    bits.push(format!("↓ {below} more"));
                }
                col = col.child(line(vec![span(
                    format!(
                        "  {} of the memory itemization — m pages it",
                        bits.join(" · ")
                    ),
                    faint,
                )]));
            }
            col.build()
        },
    ));

    Element::new()
        .style(LayoutStyle::column().gap(0))
        .child(strip.build())
        .child(
            Tabs::new()
                .tab("Loaded", move || {
                    models_table(cx, &tt, &models, ui.model_sel, autofocus)
                })
                .tab("Caches", move || {
                    caches_table(cx, &tt, &caches, ui.cache_sel)
                })
                .active(ui.models_tab)
                .layout(LayoutStyle::column().grow(1.0))
                .element(cx, t)
                .build(),
        )
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move |
        | {
            // The selected row's detail: the toned modality BADGE (a
            // Table cell is a string, so the chip lives here), the
            // full identifiers the columns may have cut, and the state
            // facts that earn no column of their own.
            let t = tt;
            if ui.models_tab.get() == 0 {
                let Some(r) = models_detail.get(ui.model_sel.get()) else {
                    return line(vec![span(String::new(), t.text)]);
                };
                let (tone, label) = task_tone(r.task.as_deref());
                let mut bits: Vec<String> = Vec::new();
                bits.push(format!(
                    "{} / {}",
                    r.provider.as_deref().unwrap_or("—"),
                    r.model.as_deref().unwrap_or("—")
                ));
                // What `k` DOES on THIS row — the lock affordance is per
                // row (every resident line has one, adoption included),
                // so the hint is per row too, and a refusal names why.
                // It rides HIGH in the line: the softer facts below are
                // the ones a narrow terminal may truncate away.
                bits.push(match lock_action(r) {
                    LockAction::Unlock => "k unlocks".to_string(),
                    LockAction::Lock { adopt: true } => "k locks (adopts it)".to_string(),
                    LockAction::Lock { adopt: false } => "k locks".to_string(),
                    LockAction::Refused(why) => format!("no lock ({why})"),
                });
                if let Some(st) = &r.state {
                    bits.push(st.clone());
                }
                if r.pinned == Some(true) {
                    bits.push("pinned".into());
                }
                if let Some(h) = &r.host_name {
                    bits.push(format!("host {h}"));
                }
                if let Some(lu) = &r.last_used_at {
                    bits.push(format!("last used {lu}"));
                }
                bits.push("e estimates context".into());
                Element::new()
                    .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
                    .child(badge(&t, label, tone))
                    .child(line(vec![span(bits.join("  ·  "), t.text_muted)]))
                    .build()
            } else {
                let Some(r) = caches_detail.get(ui.cache_sel.get()) else {
                    return line(vec![span(String::new(), t.text)]);
                };
                line(vec![
                    span_bold(format!(" {} ", r.key), t.accent),
                    span("· c clears every cache of its session", t.text_faint),
                ])
            }
        }))
        .build()
}

/// THE PINNED HEAD: the RAM gauge (the PRIMARY system meter — "how full
/// is this machine"), the accelerator-heap gauge under its own scoped
/// label with the GGUF note beneath it, the GPU utilization gauge (only
/// when supported), and one muted note per DEGRADED section so a
/// half-blind snapshot says so instead of rendering as a healthy blank.
///
/// These rows never scroll and are never windowed: they are the answer to
/// the question the screen exists to answer, and a reading that can be
/// scrolled away is a reading the operator cannot trust. Host identity and
/// the itemization live in [`detail_rows`], which IS windowed — identity
/// is a label, not a measurement.
///
/// Each returned view is exactly one row. `body` counts them to size the
/// itemization's budget, so nothing here may render a variable number of
/// lines from one entry.
fn head_rows(t: &TokenSet, d: &HostStateData) -> Vec<View> {
    let mut rows: Vec<View> = Vec::new();
    if let Some(ram) = &d.ram {
        let frac = ram
            .percent
            .map(|p| (p / 100.0) as f32)
            .or_else(|| match (ram.used_bytes, ram.total_bytes) {
                (Some(u), Some(total)) if total > 0 => Some(u as f32 / total as f32),
                _ => None,
            });
        if let Some(frac) = frac {
            let mut text = format!("{:.0}%", f64::from(frac) * 100.0);
            if let (Some(u), Some(total)) = (ram.used_bytes, ram.total_bytes) {
                text.push_str(&format!(" · {} / {}", human_bytes(u), human_bytes(total)));
            }
            if let Some(a) = ram.available_bytes {
                text.push_str(&format!(" · {} free", human_bytes(a)));
            }
            rows.push(gauge_row(t, "RAM", frac, text));
        } else {
            rows.push(line(vec![span(
                "RAM: reported without usable numbers",
                t.text_muted,
            )]));
        }
    }
    // THE ACCELERATOR HEAP — its own clearly-scoped line (SPEC PART A2),
    // never presented as the machine's memory. The all-processes reading
    // wins, the process-local pair is the LABELLED fallback, and the
    // label always names the scope: `allocated_bytes` is process-local
    // on Metal — it reads 0 while a 93 GiB GGUF is resident, which is
    // exactly how this meter came to show "0 B". The note rides under
    // the bar because the counter is BLIND to mmapped GGUF weights.
    if let Some(dev) = &d.device {
        let backend = if dev.backend.trim().is_empty() {
            "device".to_string()
        } else {
            dev.backend.clone()
        };
        match dev.accelerator() {
            Some((used, ceiling, _)) => {
                let mut text = match ceiling {
                    Some(c) => format!("{} / {}", human_bytes(used), human_bytes(c)),
                    None => human_bytes(used),
                };
                if let Some(f) = dev.free_bytes {
                    text.push_str(&format!(" · {} free", human_bytes(f)));
                }
                // The label owns a full line: at 40 cells it would eat
                // the bar's column, and the scope words are the whole
                // point of the line.
                rows.push(line(vec![span(
                    dev.label().unwrap_or_default(),
                    t.text_muted,
                )]));
                match ceiling {
                    // Empty label, same 7-cell field: the bar lands in
                    // the SAME column as the RAM bar above it.
                    Some(c) => rows.push(gauge_row(t, "", used as f32 / c as f32, text)),
                    None => rows.push(field_w(t, "", 7, line(vec![span(text, t.text_muted)]))),
                }
                rows.push(field_w(
                    t,
                    "",
                    7,
                    line(vec![span(ACCELERATOR_NOTE, t.text_faint)]),
                ));
            }
            None => {
                rows.push(line(vec![span(
                    format!("{backend}: allocation unreported"),
                    t.text_muted,
                )]));
            }
        }
    }
    // The GPU gauge exists ONLY when the probe says supported — an
    // unsupported host gets the degradation note below, never a dead
    // 0% bar.
    if d.gpu_supported {
        match d.gpu_util_pct {
            Some(pct) => {
                rows.push(gauge_row(
                    t,
                    "GPU",
                    (pct / 100.0) as f32,
                    format!("{pct:.0}% utilization"),
                ));
            }
            None => {
                rows.push(line(vec![span(
                    "GPU supported — utilization unreported",
                    t.text_muted,
                )]));
            }
        }
    }
    // A degraded section explains a meter that is MISSING above it, so it
    // is pinned with the meters. Windowed, it would read as an absence.
    for section in &d.degraded {
        let why = d
            .reasons
            .get(section)
            .cloned()
            .unwrap_or_else(|| "the host could not answer this section".into());
        rows.push(line(vec![span(
            format!("⚠ {section} degraded — {why}"),
            t.text_muted,
        )]));
    }
    rows
}

/// THE WINDOWED DETAIL: host identity, then WHAT IS CONSUMING THE MEMORY
/// (SPEC PART B) — the ITEMS the framework can name, then, behind a rule
/// so no reader adds them together, the REFERENCE counters, then the GGUF
/// note when the weights exceed the accelerator heap. The per-model list
/// is capped so a host holding a dozen models cannot squeeze the table off
/// screen; the cap NAMES what it hid.
///
/// Returned as (text, ink) pairs rather than views because `body` WINDOWS
/// them: at 80x24 there is no room for both this and the Loaded table, and
/// the table wins (the lock verb is per row). One pair is one row — the
/// GGUF note is pre-wrapped into one pair per wrapped line — so the count
/// is the height and the `↓ N more` affordance can be honest about N.
fn detail_rows(t: &TokenSet, d: &HostStateData, wrap_w: usize) -> Vec<(String, Rgba)> {
    let mut out: Vec<(String, Rgba)> = Vec::new();
    // HOST IDENTITY ONLY. The process RSS used to ride here too, and
    // then AGAIN as the breakdown's `process_rss` item — one figure
    // stated twice in one memory panel is exactly the double-count this
    // wave exists to remove (agreed cross-surface with abstractflow,
    // which carried the same duplication). RSS is stated ONCE, as the
    // breakdown item that names what it measures.
    if let Some(h) = &d.host_name {
        out.push((format!("host: {h}"), t.text_faint));
    }
    let breakdown = memory_breakdown(d);
    if breakdown.is_empty() {
        return out;
    }
    // The cap applies to the PER-MODEL item lines only: the fixed
    // tail (caches, RSS, the references, the note) always renders.
    const CAP: usize = 6;
    let n_models = breakdown.iter().filter(|l| l.per_model).count();
    let hidden = n_models.saturating_sub(CAP);
    out.push(("consuming memory:".to_string(), t.text_muted));
    let row = |l: &crate::store::BreakdownLine| {
        let size = if l.size.is_empty() {
            "—".to_string()
        } else {
            l.size.clone()
        };
        let note = if l.note.is_empty() {
            String::new()
        } else {
            format!("  ·  {}", l.note)
        };
        format!("  {:<24} {:>19}{note}", l.label, size)
    };
    let mut shown_models = 0usize;
    for l in breakdown.iter().filter(|l| l.kind == BreakdownKind::Item) {
        if l.per_model {
            shown_models += 1;
            if shown_models > CAP {
                continue;
            }
        }
        out.push((row(l), t.text_muted));
    }
    if hidden > 0 {
        out.push((
            format!("  + {hidden} more resident model(s) — the Loaded table lists every one"),
            t.text_faint,
        ));
    }
    let refs: Vec<&crate::store::BreakdownLine> = breakdown
        .iter()
        .filter(|l| l.kind == BreakdownKind::Reference)
        .collect();
    if !refs.is_empty() {
        // THE RULE between the two halves: references are separate
        // counters measured against different denominators — adding
        // them to the items above is the error this line prevents.
        out.push((
            "  ─── for reference — separate counters, NOT summable with the items above ───"
                .to_string(),
            t.text_faint,
        ));
        for l in refs {
            out.push((row(l), t.text_faint));
        }
    }
    // The GGUF note, WRAPPED — never truncated, never reworded.
    for l in breakdown.iter().filter(|l| l.kind == BreakdownKind::Note) {
        for part in wrap_text(&l.label, wrap_w.saturating_sub(2)) {
            out.push((format!("  {part}"), t.text_muted));
        }
    }
    out
}

/// One gauge row: muted label, ramped Progress bar (ok → warn → error
/// as it fills — usage-meter semantics), facts beside it.
fn gauge_row(t: &TokenSet, label: &str, frac: f32, text: String) -> View {
    field_w(
        t,
        label,
        7,
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1))
            .child(
                Progress::new(frac)
                    .ramp(true)
                    .thresholds(0.75, 0.9)
                    .layout(LayoutStyle::default().w(24).h(1).shrink(0.0))
                    .element(t)
                    .build(),
            )
            .child(line(vec![span(text, t.text_muted)]))
            .build(),
    )
}

fn models_table(
    cx: Scope,
    t: &TokenSet,
    data: &[ModelRow],
    sel: Signal<usize>,
    autofocus: bool,
) -> View {
    if data.is_empty() {
        return line(vec![span(
            "∅ no models resident — w warms one up",
            t.text_muted,
        )]);
    }
    let w = abstracttui::app::use_viewport(cx).get().w;
    let mut rows: Vec<Vec<String>> = data.iter().map(model_row_cells).collect();
    // Modality/resident/size/ctx/lock/default print closed vocabularies
    // (head-fit floors sized to their widest word); provider and model
    // are identifiers that discriminate on their TAIL.
    let rules = [
        widths::ColRule::head("modality", 9),
        widths::ColRule::tail("provider", 10),
        widths::ColRule::tail("model", 22),
        widths::ColRule::head("resident", 8),
        widths::ColRule::head("size", 10),
        widths::ColRule::head("cache", 8),
        widths::ColRule::head("ctx", 7),
        widths::ColRule::head("lock", 4),
        widths::ColRule::head("default", 7),
    ];
    let cols = widths::columns(&rules, &mut rows, w - BLOCK_CHROME);
    let el = Table::new(cols)
        .rows(rows)
        .selection(sel)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t);
    if autofocus {
        el.autofocus().build()
    } else {
        el.build()
    }
}

fn caches_table(cx: Scope, t: &TokenSet, data: &[SessionCacheRow], sel: Signal<usize>) -> View {
    if data.is_empty() {
        return line(vec![span(
            "∅ no session prompt caches on the host",
            t.text_muted,
        )]);
    }
    let w = abstracttui::app::use_viewport(cx).get().w;
    let mut rows: Vec<Vec<String>> = data.iter().map(cache_row_cells).collect();
    let rules = [
        widths::ColRule::tail("key", 16),
        widths::ColRule::tail("session", 10),
        widths::ColRule::tail("model", 14),
        widths::ColRule::head("size", 9),
        widths::ColRule::head("tokens", 7),
    ];
    let cols = widths::columns(&rules, &mut rows, w - BLOCK_CHROME);
    Table::new(cols)
        .rows(rows)
        .selection(sel)
        .layout(LayoutStyle::default().grow(1.0))
        .element(cx, t)
        .build()
}

// ---------------------------------------------------------------------
// Actions (refusals name their reasons — the F2/F3 law)
// ---------------------------------------------------------------------

/// Send a model mutation with its busy entry opened AT ENQUEUE. The
/// worker lane is serial and a silent steady-state host-state poll can
/// hold it for a whole slow GET — a confirmed action that showed
/// nothing until dequeue read as dead. The op id rides the Cmd; the
/// worker's `finish_busy` closes it when the work completes (and the
/// worker's panic path clears the whole strip, so it cannot leak).
fn send_mutation(ctx: &Ctx, label: String, make: impl FnOnce(u64) -> Cmd) {
    let op = crate::worker::next_op();
    ctx.store.begin_busy(op, &label);
    ctx.send(make(op));
}

fn selected_model(ctx: &Ctx) -> Option<ModelRow> {
    let idx = ctx.ui.model_sel.get_untracked();
    ctx.store
        .host_state
        .with_untracked(|d| d.ready().and_then(|d| d.models.get(idx).cloned()))
}

/// The (provider, model) a mutation can target — both halves required
/// (the API addresses models by the pair).
fn model_pair(row: &ModelRow) -> Option<(String, String)> {
    match (&row.provider, &row.model) {
        (Some(p), Some(m)) => Some((p.clone(), m.clone())),
        _ => None,
    }
}

/// `u` — unload the selected model (danger-confirmed). A LOCKED model
/// is sent anyway with force:false: the gateway's 409 is the authority
/// on the lock, and its refusal triggers the "Force unload?" second
/// confirm (the row's own `locked` may be stale) — the confirm text
/// forewarns when the row already says locked.
fn unload_selected(cx: Scope, ctx: &Ctx) {
    let Some(row) = selected_model(ctx) else {
        ctx.store
            .notice
            .set(Some("no model selected — nothing to unload".into()));
        return;
    };
    let Some((p, m)) = model_pair(&row) else {
        ctx.store.notice.set(Some(
            "this row names no provider/model pair — the gateway cannot target it".into(),
        ));
        return;
    };
    // A row the host says is NOT resident has nothing to unload. An
    // UNKNOWN residency still may — the tri-state's third answer is not
    // a "no", and the gateway is the authority on what it holds.
    if let Some(why) = unload_refusal(&row) {
        ctx.store.notice.set(Some(format!("{p}/{m}: {why}")));
        return;
    }
    let locked_hint = if row.locked == Some(true) {
        " It is LOCKED — the gateway will refuse and offer a force unload."
    } else {
        ""
    };
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!("Unload {p}/{m} from the execution host?{locked_hint}"),
        "Unload",
        "Keep it loaded",
        move || {
            send_mutation(&ctx2, format!("unloading {p}/{m}"), |op| Cmd::UnloadModel {
                provider: p,
                model: m,
                force: false,
                op,
            });
        },
    );
}

/// `k` — toggle the residency lock. Locking is safe (no confirm);
/// UNLOCKING a locked model removes its protection and confirms.
///
/// EVERY resident line offers this verb, sweep/externally-loaded rows
/// included: `POST /models/lock` ADOPTS a model LM Studio or ollama
/// loaded, so `lockable: null` is an unknown the gateway answers, never
/// a refusal we invent. [`lock_action`] is the single authority — the
/// same one the row's hint line reads.
fn toggle_lock_selected(cx: Scope, ctx: &Ctx) {
    let Some(row) = selected_model(ctx) else {
        ctx.store
            .notice
            .set(Some("no model selected — nothing to lock".into()));
        return;
    };
    let Some((p, m)) = model_pair(&row) else {
        ctx.store.notice.set(Some(
            "this row names no provider/model pair — the gateway cannot target it".into(),
        ));
        return;
    };
    match lock_action(&row) {
        LockAction::Unlock => {
            let ctx2 = ctx.clone();
            super::confirm_danger(
                cx,
                ctx.ui,
                format!("Unlock {p}/{m}? An unlocked model can be evicted or unloaded."),
                "Unlock",
                "Keep the lock",
                move || {
                    send_mutation(&ctx2, format!("unlocking {p}/{m}"), |op| Cmd::LockModel {
                        provider: p,
                        model: m,
                        lock: false,
                        op,
                    });
                },
            );
        }
        LockAction::Lock { adopt } => {
            let label = if adopt {
                format!("locking (adopting) {p}/{m}")
            } else {
                format!("locking {p}/{m}")
            };
            send_mutation(ctx, label, |op| Cmd::LockModel {
                provider: p,
                model: m,
                lock: true,
                op,
            });
        }
        LockAction::Refused(why) => {
            ctx.store.notice.set(Some(format!("{p}/{m}: {why}")));
        }
    }
}

/// The CUSTOM sentinel of the provider picker: a gateway can hold a
/// provider discovery never listed, and a picker with no escape hatch
/// would make that model unloadable from this screen.
const CUSTOM_PROVIDER: &str = "type a provider not listed…";

/// The model catalog for one provider, REUSING what this crate already
/// fetches: the provider row's own `models` when the discovery payload
/// carried them (`/discovery/providers?include_models=true`), otherwise
/// the per-provider catalog (`/discovery/providers/{p}/models`) cached
/// under `store.models`. A `Failed` catalog is NOT "no models" — the
/// caller keeps the honest free-text lane and says why.
fn catalog_for(store: &crate::store::Store, provider: &str) -> Loadable<Vec<String>> {
    let seeded = store.providers.with_untracked(|p| {
        p.ready().and_then(|d| {
            d.items
                .iter()
                .find(|i| i.name == provider)
                .filter(|i| !i.models.is_empty())
                .map(|i| i.models.clone())
        })
    });
    match seeded {
        Some(models) => Loadable::Ready(models),
        None => store
            .models
            .with_untracked(|m| m.get(provider).cloned())
            .unwrap_or(Loadable::NotAsked),
    }
}

/// `w` — warm up (load) a model: provider and model are PICKERS over the
/// gateway's own catalogs (never free text the operator has to spell),
/// with the model list refreshed from the chosen provider and a custom
/// lane for anything discovery does not list. Prefilled from the
/// highlighted row when there is one. The optional lock-after-load rides
/// the same POST (`lock: true`).
fn open_warmup_form(cx: Scope, ctx: &Ctx) {
    let prefill = selected_model(ctx).and_then(|r| model_pair(&r));
    let store = ctx.store;
    // The catalog may never have been fetched (this tab is reachable
    // without visiting Providers) — ask for it, honestly, at open.
    if matches!(store.providers.get_untracked(), Loadable::NotAsked) {
        store.providers.set(Loadable::Loading);
        ctx.send(Cmd::LoadProviders);
    }
    let ctx2 = ctx.clone();
    super::open_form(ctx, cx, Size::new(72, 16), move |mcx, close| {
        let theme = use_theme(mcx);
        let t0 = theme.get().tokens;
        // Provider options: [placeholder] + discovered + CUSTOM. Read
        // once per form open — a picker whose indices shift under the
        // operator mid-selection is a fabricated pick waiting to happen.
        let mut prov_options: Vec<String> = super::providers::provider_names(&store);
        if let Some((p, _)) = &prefill {
            if !p.is_empty() && !prov_options.iter().any(|x| x == p) {
                prov_options.insert(0, p.clone());
            }
        }
        let custom_row = prov_options.len() + 1;
        let prov_ix = mcx.signal(
            prefill
                .as_ref()
                .and_then(|(p, _)| prov_options.iter().position(|x| x == p))
                .map(|i| i + 1)
                .unwrap_or(0),
        );
        let prov_custom = mcx.signal(String::new());
        // usize::MAX = "resolve the prefilled model against the list the
        // moment it lands" (the routes.rs sentinel).
        let model_ix = mcx.signal(if prefill.is_some() { usize::MAX } else { 0 });
        let model_custom = mcx.signal(
            prefill
                .as_ref()
                .map(|(_, m)| m.clone())
                .unwrap_or_default(),
        );
        let lock_after = mcx.signal(false);
        let form_error = mcx.signal(Option::<String>::None);

        // Fetch the chosen provider's catalog. UNTRACKED cache read: a
        // tracked one would re-fire on the failure landing = a retry
        // loop (the routes.rs law, same reason).
        {
            let prov_options = prov_options.clone();
            let ctx3 = ctx2.clone();
            mcx.effect(move || {
                let ix = prov_ix.get();
                if ix == 0 || ix >= custom_row {
                    return;
                }
                let name = prov_options[ix - 1].clone();
                if matches!(catalog_for(&store, &name), Loadable::Ready(_)) {
                    return;
                }
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
        // Resolve the prefilled model against its own provider's list —
        // and only its own: a saved model under another provider is a
        // fabricated pair, so it resolves to the placeholder.
        {
            let prov_options = prov_options.clone();
            let prefill2 = prefill.clone();
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
                // Track the map so this re-runs when the catalog lands.
                let _ = store.models.with(|m| m.len());
                match catalog_for(&store, &name) {
                    Loadable::Ready(models) if !models.is_empty() => {
                        let pos = prefill2
                            .as_ref()
                            .filter(|(p, _)| *p == name)
                            .and_then(|(_, m)| models.iter().position(|x| x == m))
                            .map(|i| i + 1);
                        model_ix.set(pos.unwrap_or(0));
                    }
                    Loadable::Ready(_) | Loadable::Failed(_) => model_ix.set(0),
                    _ => {}
                }
            });
        }

        let prov_select_options: Vec<SelectOption> =
            std::iter::once(SelectOption::new("choose a provider…"))
                .chain(prov_options.iter().map(|p| SelectOption::new(p.clone())))
                .chain(std::iter::once(SelectOption::new(CUSTOM_PROVIDER)))
                .collect();
        let prov_options_pick = prov_options.clone();
        let prov_options_send = prov_options.clone();
        let ctx3 = ctx2.clone();
        let close_ok = close.clone();
        let close_cancel = close.clone();
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .child(line(vec![span_bold("Load (warm up) a model", t0.accent)]))
            .child(line(vec![span(
                "the gateway pulls the model into host memory — a cold load can take a while",
                t0.text_faint,
            )]))
            .child(field(
                &t0,
                "provider",
                Select::new(prov_select_options)
                    .value(prov_ix)
                    .on_change(move |_| {
                        // A provider switch RESETS the model picker —
                        // never a pair the catalogs never served.
                        model_ix.set(0);
                        model_custom.set(String::new());
                    })
                    .layout(LayoutStyle::default().w(40).h(1).shrink(0.0))
                    .element(mcx, &t0)
                    .autofocus()
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
                        .placeholder("e.g. lmstudio, mlx, ollama")
                        .placeholder_while_focused(true)
                        .layout(LayoutStyle::default().w(40).h(1))
                        .element(g2, &t)
                        .build(),
                )
            }))
            // Model row: the provider's catalog when it answered, an
            // honest free-text lane (with the reason) when it did not.
            .child(dyn_view_scoped(LayoutStyle::column(), move |g2| {
                let t = theme.get().tokens;
                let ix = prov_ix.get();
                if ix == 0 {
                    return field(
                        &t,
                        "model",
                        line(vec![span("choose a provider first", t.text_faint)]),
                    );
                }
                if ix >= custom_row {
                    return field(
                        &t,
                        "model",
                        TextInput::new()
                            .value(model_custom)
                            .placeholder("model id for that provider")
                            .placeholder_while_focused(true)
                            .layout(LayoutStyle::default().w(50).h(1))
                            .element(g2, &t)
                            .build(),
                    );
                }
                // Track the catalog map so this region re-renders when
                // the list lands.
                let _ = store.models.with(|m| m.len());
                let name = prov_options_pick[ix - 1].clone();
                match catalog_for(&store, &name) {
                    Loadable::Ready(models) if !models.is_empty() => {
                        let opts: Vec<SelectOption> =
                            std::iter::once(SelectOption::new("choose a model…"))
                                .chain(models.iter().map(|m| SelectOption::new(m.clone())))
                                .collect();
                        field(
                            &t,
                            "model",
                            Combobox::new(opts)
                                .value(model_ix)
                                .placeholder("type to filter models…")
                                .layout(LayoutStyle::default().w(50).h(1).shrink(0.0))
                                .element(g2, &t)
                                .build(),
                        )
                    }
                    Loadable::Loading | Loadable::NotAsked => field(
                        &t,
                        "model",
                        line(vec![span("⟳ discovering models…", t.info)]),
                    ),
                    // Discovery FAILED ≠ "this provider has no models":
                    // name the error and keep the free-text lane.
                    Loadable::Failed(e) => Element::new()
                        .style(LayoutStyle::column())
                        .child(field(
                            &t,
                            "model",
                            TextInput::new()
                                .value(model_custom)
                                .placeholder("discovery failed — type the model id")
                                .placeholder_while_focused(true)
                                .layout(LayoutStyle::default().w(50).h(1))
                                .element(g2, &t)
                                .build(),
                        ))
                        .child(field(
                            &t,
                            "",
                            line(vec![span(
                                format!("model discovery failed: {}", e.message),
                                t.error,
                            )]),
                        ))
                        .build(),
                    Loadable::Ready(_) => field(
                        &t,
                        "model",
                        TextInput::new()
                            .value(model_custom)
                            .placeholder("no discoverable models — type the model id")
                            .placeholder_while_focused(true)
                            .layout(LayoutStyle::default().w(50).h(1))
                            .element(g2, &t)
                            .build(),
                    ),
                }
            }))
            .child(field(
                &t0,
                "",
                Checkbox::new("lock after load (pin against eviction)")
                    .checked(lock_after)
                    .element(mcx, &t0)
                    .build(),
            ))
            .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                match form_error.get() {
                    Some(e) => line(vec![span_bold(format!("✗ {e}"), t0.error)]),
                    None => line(vec![span(String::new(), t0.text)]),
                }
            }))
            .child(
                Element::new()
                    .style(LayoutStyle::row().gap(2).h(1).shrink(0.0))
                    .child(
                        Button::new("Load")
                            .on_click(move || {
                                let Some((p, m)) = picked_pair(
                                    &store,
                                    &prov_options_send,
                                    custom_row,
                                    prov_ix,
                                    prov_custom,
                                    model_ix,
                                    model_custom,
                                ) else {
                                    form_error.set(Some(
                                        "pick a provider and a model — both name the target"
                                            .into(),
                                    ));
                                    return;
                                };
                                send_mutation(&ctx3, format!("loading model {p}/{m}"), |op| {
                                    Cmd::WarmupModel {
                                        task: None, // gateway defaults to text_generation
                                        provider: p,
                                        model: m,
                                        lock: lock_after.get_untracked(),
                                        op,
                                    }
                                });
                                close_ok();
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

/// The (provider, model) the two pickers currently name — `None` while
/// either half is unpicked/blank. The custom lanes win over the catalog
/// index for their own row; a catalog pick reads the LIST, never the
/// text box, so the two lanes can never blend into a pair nobody chose.
fn picked_pair(
    store: &crate::store::Store,
    prov_options: &[String],
    custom_row: usize,
    prov_ix: Signal<usize>,
    prov_custom: Signal<String>,
    model_ix: Signal<usize>,
    model_custom: Signal<String>,
) -> Option<(String, String)> {
    let ix = prov_ix.get_untracked();
    let (provider, from_catalog) = if ix >= custom_row {
        (prov_custom.get_untracked().trim().to_string(), false)
    } else if ix == 0 {
        return None;
    } else {
        (prov_options.get(ix - 1)?.clone(), true)
    };
    if provider.is_empty() {
        return None;
    }
    let model = if from_catalog {
        match catalog_for(store, &provider) {
            Loadable::Ready(models) if !models.is_empty() => {
                let mix = model_ix.get_untracked();
                if mix == 0 || mix == usize::MAX {
                    return None;
                }
                models.get(mix - 1)?.clone()
            }
            _ => model_custom.get_untracked().trim().to_string(),
        }
    } else {
        model_custom.get_untracked().trim().to_string()
    };
    (!model.is_empty()).then_some((provider, model))
}

/// `e` — context estimate for the selected row. The answer (confidence
/// + predicted max + first note) lands as a notice line.
fn estimate_selected(ctx: &Ctx) {
    let Some(row) = selected_model(ctx) else {
        ctx.store
            .notice
            .set(Some("no model selected — nothing to estimate".into()));
        return;
    };
    let Some((p, m)) = model_pair(&row) else {
        ctx.store.notice.set(Some(
            "this row names no provider/model pair — nothing to estimate".into(),
        ));
        return;
    };
    send_mutation(ctx, format!("context estimate {p}/{m}"), |op| {
        Cmd::ContextEstimate {
            provider: p,
            model: m,
            context_length: row.context_length,
            op,
        }
    });
}

/// `c` — clear every prompt cache of the selected cache row's session
/// (Caches sub-tab only; danger-confirmed).
fn clear_caches_selected(cx: Scope, ctx: &Ctx) {
    if ctx.ui.models_tab.get_untracked() != 1 {
        ctx.store.notice.set(Some(
            "switch to the Caches tab — c clears the selected session's caches there".into(),
        ));
        return;
    }
    let idx = ctx.ui.cache_sel.get_untracked();
    let row = ctx
        .store
        .host_state
        .with_untracked(|d| d.ready().and_then(|d| d.caches.get(idx).cloned()));
    let Some(row) = row else {
        ctx.store
            .notice
            .set(Some("no cache selected — nothing to clear".into()));
        return;
    };
    if row.session_id.is_empty() {
        ctx.store.notice.set(Some(
            "this cache names no session — the clear-all endpoint cannot target it".into(),
        ));
        return;
    }
    let sid = row.session_id.clone();
    let ctx2 = ctx.clone();
    super::confirm_danger(
        cx,
        ctx.ui,
        format!(
            "Clear ALL prompt caches for session '{sid}'? Cached prompts rebuild on next use."
        ),
        "Clear the caches",
        "Keep them",
        move || {
            send_mutation(&ctx2, format!("clearing caches of '{sid}'"), |op| {
                Cmd::ClearSessionCaches {
                    session_id: sid,
                    op,
                }
            });
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(v: serde_json::Value) -> ModelRow {
        ModelRow::from_value(&v)
    }

    /// The tone table pinned verbatim: the labels are the gateway's
    /// modality_ui vocabulary; a null task is the muted "?" (unknown,
    /// never guessed).
    #[test]
    fn task_tone_maps_the_modality_ui_vocabulary() {
        assert_eq!(task_tone(Some("text_generation")), (Tone::Accent, "Text"));
        for t in ["image_generation", "image_to_image", "image_upscale"] {
            assert_eq!(task_tone(Some(t)), (Tone::Ok, "Image"), "{t}");
        }
        for t in ["video_generation", "text_to_video", "image_to_video"] {
            assert_eq!(task_tone(Some(t)), (Tone::Info, "Video"), "{t}");
        }
        assert_eq!(task_tone(Some("tts")), (Tone::Info, "Voice"));
        assert_eq!(task_tone(Some("stt")), (Tone::Info, "Voice"));
        assert_eq!(task_tone(Some("music_generation")), (Tone::Warn, "Music"));
        for t in ["scene3d_generation", "text_to_scene3d", "image_to_scene3d"] {
            assert_eq!(task_tone(Some(t)), (Tone::Muted, "3D"), "{t}");
        }
        assert_eq!(task_tone(Some("embedding")), (Tone::Muted, "Embedding"));
        assert_eq!(task_tone(None), (Tone::Muted, "?"));
        assert_eq!(task_tone(Some("someday_new_task")), (Tone::Muted, "?"));
    }

    /// Row cells: tri-state resident, the calibration star, the size
    /// coalesce with its `~` estimate marker, the per-model cache
    /// column, the ⊘ lock marker (never the padlock emoji).
    #[test]
    fn model_row_cells_render_the_facts() {
        let full = row(serde_json::json!({
            "task": "text_generation", "provider": "mlx", "model": "qwen",
            "resident": true, "locked": true, "lockable": true, "default": true,
            "size_bytes": 2147483648u64, "cache_bytes": 1073741824u64,
            "context_length": 8192u64, "context_calibrated": true
        }));
        assert_eq!(
            model_row_cells(&full),
            vec!["Text", "mlx", "qwen", "yes", "2.0 GiB", "1.0 GiB", "8192*", "⊘", "✓"]
        );

        // resident: null renders the DISTINCT third state; an
        // uncalibrated context prints no star; VRAM size is the
        // fallback when no RAM size was reported.
        let unknown = row(serde_json::json!({
            "task": null, "provider": "lmstudio", "model": "mystery",
            "resident": null, "size_vram_bytes": 1024u64, "context_length": 4096u64
        }));
        let cells = model_row_cells(&unknown);
        assert_eq!(cells[0], "?", "null task is the muted unknown");
        assert_eq!(cells[3], "unknown", "null resident is a third state");
        assert_eq!(cells[4], "1.0 KiB", "vram size is the fallback");
        assert_eq!(cells[5], "—", "no cache reported: the dash, never a 0");
        assert_eq!(cells[6], "4096", "no star without calibration");
        assert_eq!(cells[7], "", "lock unknown renders blank, never locked");
        assert_eq!(cells[8], "", "not default renders blank");

        // The sweep row that used to render BLANK: only an estimate —
        // it renders, MARKED, so an estimate is never read as measured.
        // The WIRE shape for such a row is `source: "provider_server"`
        // with `lockable: true` (the sweep stamps it), which is why the
        // adopt wording keys off the SOURCE and never off `lockable`.
        let swept = row(serde_json::json!({
            "provider": "lmstudio", "model": "glm-4.6-gguf",
            "source": "provider_server", "resident": true, "lockable": true,
            "est_weights_bytes": 99857989632u64, "cache_bytes": 2147483648u64
        }));
        let cells = model_row_cells(&swept);
        assert_eq!(cells[4], "~93.0 GiB", "estimated size is marked with ~");
        assert_eq!(cells[5], "2.0 GiB");
        assert_eq!(cells[7], "", "lockable:true is not LOCKED — the cell stays blank");
        assert_eq!(
            lock_action(&swept),
            LockAction::Lock { adopt: true },
            "and `k` on it ADOPTS: the detail line's adopt arm has a row"
        );

        // No sizes at all → the honest dash.
        let bare = row(serde_json::json!({"provider": "p", "model": "m"}));
        assert_eq!(model_row_cells(&bare)[4], "—");
        assert_eq!(model_row_cells(&bare)[5], "—");
        assert_eq!(model_row_cells(&bare)[6], "—");
    }

    #[test]
    fn cache_row_cells_render_the_facts() {
        let r = SessionCacheRow {
            key: "agw.pc.v1.s-sess1:session".into(),
            provider: "mlx".into(),
            model: "qwen".into(),
            session_id: "sess1".into(),
            bytes: Some(4096),
            token_count: Some(100),
        };
        assert_eq!(
            cache_row_cells(&r),
            vec!["agw.pc.v1.s-sess1:session", "sess1", "qwen", "4.0 KiB", "100"]
        );
        let bare = SessionCacheRow {
            key: "k".into(),
            ..SessionCacheRow::default()
        };
        assert_eq!(cache_row_cells(&bare), vec!["k", "—", "—", "—", "—"]);
    }

    /// Totals: counts from the rows, bytes from the sum-of-known rule
    /// (None → "—", never a fabricated 0). Resident is counted from
    /// `resident == Some(true)` ONLY — an unknown/absent residency never
    /// inflates the "resident" figure (default ≠ loaded).
    #[test]
    fn totals_line_says_counts_and_honest_bytes() {
        let mut d = HostStateData::default();
        assert_eq!(
            totals_line(&d),
            "totals: 0 resident / 0 model row(s) · — · 0 session cache(s) · —"
        );
        d.models.push(row(serde_json::json!({"provider": "p", "model": "m",
                                             "size_bytes": 1024u64})));
        d.caches.push(SessionCacheRow {
            key: "k".into(),
            bytes: Some(2048),
            ..SessionCacheRow::default()
        });
        d.recount();
        // The row carries NO residency claim: it counts as a row, never as
        // resident.
        assert_eq!(
            totals_line(&d),
            "totals: 0 resident / 1 model row(s) · 1.0 KiB · 1 session cache(s) · 2.0 KiB"
        );
        d.models.push(row(serde_json::json!({"provider": "p", "model": "m2",
                                             "resident": true})));
        d.recount();
        assert_eq!(
            totals_line(&d),
            "totals: 1 resident / 2 model row(s) · 1.0 KiB · 1 session cache(s) · 2.0 KiB"
        );
    }
}
