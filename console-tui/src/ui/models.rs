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
use abstracttui::widgets::{Progress, Tone};

use super::util::{badge, field, field_w, line, span, span_bold, wrap_text};
use super::w::action::{button, On};
use super::w::{Action, Cell, Col, ColW, DataTable, Row as WRow};
use super::widths::BLOCK_CHROME;
use super::Ctx;
use crate::store::operator::{HostRunner, HostUpdate};
use crate::store::ConnPhase;
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

/// The footer verbs of this screen that only an admin may use: the
/// residency mutations (`/models/load|unload|lock|unlock`) and the
/// enumeration-based cache clear (`/sessions/{id}/prompt_cache/clear_all`)
/// — the web renders them for admins only (renderModelsLoadForm,
/// renderModelsTable, renderSessionCaches). Reads and the context
/// estimate stay open to every principal.
pub const ADMIN_KEYS: &[&str] = &["u", "k", "w", "c"];

// ---------------------------------------------------------------------
// R15 Resources (DESIGN-TUI.md §3.11): the web's page — ◎ Gateway (how
// this gateway runs: Workflows paused, Version + Check now / Update,
// Desktop icon, Last restart, Start at login, Restart gateway… / Quit
// gateway…), ▦ Memory & GPU (the gauges and the itemization), ▣ Models
// (the "Show configured / cached" toggle, [Load model], ONE DataTable
// whose rows carry the web's Estimate / Lock|Unlock / Unload buttons) and
// ⌸ Session caches (a DataTable with Clear). Every action is a click AND a
// key; every confirm is w::Confirm with the web's sentence.
// ---------------------------------------------------------------------

/// The page title and subtitle (the web's tab heading).
pub const TITLE: &str = "Resources";
pub const SUBTITLE: &str = "Host resources: loaded models, memory and GPU, session caches";
/// The section headings and notes (the web's words).
pub const GATEWAY_TITLE: &str = "Gateway";
pub const GATEWAY_NOTE: &str = "How this gateway is running right now. Pausing stops new workflow steps; the console and connected apps keep answering.";
pub const MEMORY_TITLE: &str = "Memory & GPU";
pub const MODELS_TITLE: &str = "Models";
pub const CACHES_TITLE: &str = "Session caches";
pub const REFRESH_TIP: &str = "Reload the loaded models, memory and GPU state";
pub const PAUSE_LABEL: &str = "Workflows paused";
pub const PAUSE_TIP: &str = "On: no new workflow step starts until you switch it off; work already inside a call finishes first";
pub const CHECK_TIP: &str = "Check for a newer release: an AbstractFramework installer install compares with the newest AbstractFramework release, any other install with the newest AbstractGateway on PyPI (needs internet)";
pub const UPDATE_TIP: &str = "Install it in the background (an installer install runs the AbstractFramework installer); restart to finish";
pub const SHOW_CACHED_TIP: &str = "Also show configured / cached rows that are NOT resident in memory — informational only, nothing to unload";
pub const LOAD_TIP: &str = "Load (warm up) this model on the host now";
pub const LOCK_IN_MEMORY: &str = "lock in memory";
/// The warm-up dialog's title (the web's inline form button).
pub const LOAD_TITLE: &str = "Load model";
pub const LOCK_IN_MEMORY_TIP: &str =
    "Lock the model in memory after loading so nothing can evict it until it is unlocked";
pub const MODELS_EMPTY: &str = "No models loaded right now.";
/// The Size column's estimate marker (a TUI note: `~` = estimated).
pub const ESTIMATE_MARKER: &str = "~ = estimated size, not measured";
pub const CACHES_EMPTY: &str = "No session prompt caches right now.";
/// The confirmations (the web's `confirmAction` sentences).
pub const RESTART_QUESTION: &str = "Restart AbstractGateway? Running workflows pause at their next step and continue after the restart. The console is unavailable for a few seconds.";
pub const QUIT_QUESTION: &str = "Quit AbstractGateway? Workflows stop and this console goes offline until you start AbstractGateway again.";
pub fn unload_question(name: &str) -> String {
    format!(
        "Unload {name} from host memory? The next request that needs it pays the full load again."
    )
}
pub fn force_unload_question(name: &str) -> String {
    format!("{name} is locked in memory — the lock exists to keep it resident. Force the unload anyway?")
}
pub fn clear_cache_question(session: &str) -> String {
    format!("Clear every prompt cache for session {session}? The next turn re-encodes its prompt from scratch — nothing durable is lost.")
}

thread_local! {
    /// "Show configured / cached" (the web's `modelsShowCached`): survives
    /// a tab switch; off by default — default ≠ loaded.
    static SHOW_CACHED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// The "Show configured / cached" state (tests and the toggle).
pub fn show_cached() -> bool {
    SHOW_CACHED.with(|c| c.get())
}
pub fn set_show_cached(on: bool) {
    SHOW_CACHED.with(|c| c.set(on));
}

/// The model rows the table shows: resident rows first, then — behind the
/// toggle — the configured / cached ones (the web's `renderModelsTable`).
pub fn visible_models(rows: &[ModelRow], show: bool) -> Vec<ModelRow> {
    let mut out: Vec<ModelRow> = rows
        .iter()
        .filter(|r| r.resident == Some(true))
        .cloned()
        .collect();
    if show {
        out.extend(rows.iter().filter(|r| r.resident != Some(true)).cloned());
    }
    out
}

/// A model row's stable key (provider/model, or its index when unnamed).
pub fn model_key(r: &ModelRow, i: usize) -> String {
    match (&r.provider, &r.model) {
        (Some(p), Some(m)) => format!("{p}/{m}"),
        _ => format!("#{i}"),
    }
}

/// The page's head action (Refresh).
pub fn head_actions() -> Vec<Action> {
    vec![Action::label(
        "refresh",
        format!("{} Refresh", super::w::glyphs::glyph("rotate", true)),
    )
    .key('r')
    .tooltip(REFRESH_TIP)]
}

/// The ◎ Gateway card's buttons (admin; the web hides them otherwise):
/// Check now, Update (when the check found one), Restart gateway…, Quit
/// gateway… (refused with the gateway's reason when this launch cannot).
pub fn gateway_actions(r: Option<&HostRunner>, u: Option<&HostUpdate>, admin: bool) -> Vec<Action> {
    if !admin {
        return Vec::new();
    }
    let mut out = vec![Action::label("check", "Check now")
        .key('U')
        .tooltip(CHECK_TIP)];
    if let Some(u) = u {
        if u.update_available {
            out.push(
                Action::label("update", "Update")
                    .key('I')
                    .tooltip(UPDATE_TIP)
                    .refused(u.start_refusal()),
            );
        }
    }
    let cap_why = |ok: bool| -> Option<String> {
        match r {
            None => Some("the runner state is not loaded yet".into()),
            Some(_) if ok => None,
            Some(r) => Some(if r.cap_reason.is_empty() {
                "not available for this launch".to_string()
            } else {
                r.cap_reason.clone()
            }),
        }
    };
    out.push(
        Action::label("restart", "Restart gateway…")
            .key('R')
            .refused(cap_why(r.map(|r| r.cap_restart).unwrap_or(false))),
    );
    out.push(
        Action::label("quit", "Quit gateway…")
            .key('Q')
            .refused(cap_why(r.map(|r| r.cap_shutdown).unwrap_or(false)))
            .danger(),
    );
    out
}

/// A model row's actions (the web's order and tooltips): Estimate (a
/// provider/model pair), then for admins Lock / Unlock (lock on every
/// resident line; Unlock on any locked one) and Unload (resident rows).
pub fn row_actions(r: &ModelRow, admin: bool) -> Vec<Action> {
    let mut out = Vec::new();
    if r.provider.is_some() && r.model.is_some() {
        out.push(
            Action::label("estimate", "Estimate")
                .key('e')
                .tooltip("Ask the host how much context actually fits for this model (calibrated when it has measured)"),
        );
    }
    if !admin {
        return out;
    }
    let resident = r.resident == Some(true);
    if r.locked == Some(true) {
        out.push(Action::label("unlock", "Unlock").key('k').tooltip(if resident {
            "Release the memory lock so this model can be unloaded or evicted"
        } else {
            "Release a lock whose model is no longer in memory (the lock still blocks unloads)"
        }));
    } else if r.lockable != Some(false) && resident {
        out.push(
            Action::label("lock", "Lock")
                .key('k')
                .tooltip(if r.source.as_deref() == Some("provider_server") {
                    "Lock this model in memory — this host loaded it outside the Gateway, so locking adopts it first"
                } else {
                    "Lock this model in memory so nothing can evict it"
                }),
        );
    }
    if resident {
        out.push(
            Action::label("unload", "Unload")
                .key('u')
                .tooltip("Unload this model from host memory")
                .danger(),
        );
    }
    out
}

/// A session cache row's actions (admins): Clear.
pub fn cache_actions(c: &SessionCacheRow, admin: bool) -> Vec<Action> {
    if !admin || c.session_id.is_empty() {
        return Vec::new();
    }
    vec![Action::label("clear", "Clear")
        .key('c')
        .tooltip("Clear every prompt cache for this session")
        .danger()]
}

/// The Models section's head buttons: [Load model] (admin).
pub fn models_actions(admin: bool) -> Vec<Action> {
    if !admin {
        return Vec::new();
    }
    vec![Action::label("load", "Load model")
        .key('w')
        .tooltip(LOAD_TIP)]
}

fn page_w(cx: Scope) -> i32 {
    (crate::ui::page_viewport(cx).get().w - 2).max(20)
}

/// A short page (under 40 rows): the head is one line and the ◎ Gateway
/// card is its state line (the F3 host panel carries the rest, from every
/// screen) — the Models table's rows come first (the per-row verbs are why
/// the operator is here).
pub fn compact(cx: Scope) -> bool {
    crate::ui::page_viewport(cx).get().h < 40
}

/// One section heading line: icon + title (+ a trailing note), bold.
fn section_head(t: &TokenSet, icon: &str, title: &str) -> View {
    super::w::paint::fill_line(
        LayoutStyle::line(1).shrink(0.0),
        vec![
            super::w::Ink::new(format!("{icon} "), t.accent),
            super::w::Ink::new(title, t.text).bold(),
        ],
        None,
    )
}

/// The memory section's focus memory: the table that held the keyboard
/// keeps it across the 4 s poll's rebuilds, and never takes it from
/// elsewhere (the FocusKeeper rule, for a DataTable).
#[derive(Clone, Default)]
struct TableFocus {
    held: std::rc::Rc<std::cell::Cell<bool>>,
    ever: std::rc::Rc<std::cell::Cell<bool>>,
}

impl TableFocus {
    fn wants(&self) -> bool {
        !self.ever.get() || self.held.get()
    }
    fn wrap(&self, v: View) -> View {
        let (held, ever) = (self.held.clone(), self.ever.clone());
        Element::new()
            .style(LayoutStyle::column().grow(1.0))
            .on(abstracttui::ui::Phase::Bubble, move |_c, ev| match ev {
                abstracttui::ui::UiEvent::FocusIn => {
                    held.set(true);
                    ever.set(true);
                }
                abstracttui::ui::UiEvent::FocusOut => held.set(false),
                _ => {}
            })
            .child(v)
            .build()
    }
}

pub fn view(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    let ui = ctx.ui;
    let tt = *t;
    let show = cx.signal(show_cached());

    super::util::clamp_selection(cx, ui.model_sel, move || {
        store.host_state.with(|d| {
            d.ready()
                .map(|d| visible_models(&d.models, show.get()).len())
                .unwrap_or(0)
        })
    });
    super::util::clamp_selection(cx, ui.cache_sel, move || {
        store
            .host_state
            .with(|d| d.ready().map(|d| d.caches.len()).unwrap_or(0))
    });
    // The Gateway card's facts (runner, tray, update, start at login): read
    // once a connected principal is here (the F3 panel's loader).
    {
        let ctx_h = ctx.clone();
        let asked = cx.signal(false);
        cx.effect(move || {
            let on = store.conn.with(ConnPhase::is_connected);
            if on && !asked.get_untracked() {
                asked.set(true);
                super::host::refresh(&ctx_h);
            }
        });
    }

    // The 409 model_locked second confirm (the web's "Model locked"):
    // PAGE scope — it survives the 4 s poll's region rebuilds.
    {
        let ctx2 = ctx.clone();
        cx.effect(move || {
            let Some((p, m)) = store.unload_locked.get() else {
                return;
            };
            store.unload_locked.set(None);
            let ctx3 = ctx2.clone();
            let (p2, m2) = (p.clone(), m.clone());
            super::w::Confirm::danger(
                force_unload_question(&format!("{p}/{m}")),
                "Force unload",
                "Cancel",
            )
            .open(cx, ctx2.ui, move || {
                send_mutation(&ctx3, format!("force-unloading {p2}/{m2}"), |op| {
                    Cmd::UnloadModel {
                        provider: p2,
                        model: m2,
                        force: true,
                        op,
                    }
                });
            });
        });
    }

    // Keyed selections (the tables) synced with the legacy indices.
    let model_key_sel = cx.signal(Option::<String>::None);
    let cache_key_sel = cx.signal(Option::<String>::None);
    let vis_keys = move || -> Vec<String> {
        store.host_state.with(|d| {
            d.ready()
                .map(|d| {
                    visible_models(&d.models, show.get())
                        .iter()
                        .enumerate()
                        .map(|(i, r)| model_key(r, i))
                        .collect()
                })
                .unwrap_or_default()
        })
    };
    let cache_keys = move || -> Vec<String> {
        store.host_state.with(|d| {
            d.ready()
                .map(|d| d.caches.iter().map(|c| c.key.clone()).collect())
                .unwrap_or_default()
        })
    };
    cx.effect(move || {
        let k = model_key_sel.get();
        if let Some(i) = k.and_then(|k| vis_keys().iter().position(|x| *x == k)) {
            if ui.model_sel.get_untracked() != i {
                ui.model_sel.set(i);
            }
        }
    });
    cx.effect(move || {
        let i = ui.model_sel.get();
        if let Some(k) = vis_keys().get(i) {
            if model_key_sel.with_untracked(|c| c.as_deref() != Some(k.as_str())) {
                model_key_sel.set(Some(k.clone()));
            }
        }
    });
    cx.effect(move || {
        let k = cache_key_sel.get();
        if let Some(i) = k.and_then(|k| cache_keys().iter().position(|x| *x == k)) {
            if ui.cache_sel.get_untracked() != i {
                ui.cache_sel.set(i);
            }
        }
    });
    cx.effect(move || {
        let i = ui.cache_sel.get();
        if let Some(k) = cache_keys().get(i) {
            if cache_key_sel.with_untracked(|c| c.as_deref() != Some(k.as_str())) {
                cache_key_sel.set(Some(k.clone()));
            }
        }
    });

    let ctx_unload = ctx.clone();
    let ctx_lock = ctx.clone();
    let ctx_warm = ctx.clone();
    let ctx_est = ctx.clone();
    let ctx_clear = ctx.clone();
    let detail_top = ui.models_detail_top;
    let focus = TableFocus::default();
    let sels = (model_key_sel, cache_key_sel);
    // The page's scroll and its reveal map (focus follows scroll).
    let scroll = cx.signal(0i32);
    let visible = cx.signal(0i32);
    cx.effect(move || {
        let vp = crate::ui::page_viewport(cx).get();
        // The page head (1 line on a short page, else 2) and the totals.
        let head = if compact(cx) { 1 } else { 2 };
        visible.set((vp.h - head - 1).max(3));
    });
    let reveal = Reveal {
        scroll,
        visible,
        map: std::rc::Rc::new(std::cell::Cell::new([(0, 0); 3])),
    };
    // The selected table row follows too (arrow keys past the page's edge).
    {
        let rv = reveal.clone();
        cx.effect(move || {
            let i = ui.model_sel.get();
            rv.show_row(1, 4, i);
        });
        let rv = reveal.clone();
        cx.effect(move || {
            let i = ui.cache_sel.get();
            rv.show_row(2, 3, i);
        });
    }
    // A focused control's section comes into view when it is off-screen:
    // the focused control names itself (the status bar's focus line); the
    // tables report their own focus (`on_focus`).
    if let Some(fl) = super::w::tip::focus_line() {
        let rv = reveal.clone();
        cx.effect(move || {
            let Some(text) = fl.get() else { return };
            let gateway = [
                PAUSE_TIP,
                CHECK_TIP,
                UPDATE_TIP,
                "Restart gateway…",
                "Quit gateway…",
                "Start at login",
            ];
            let models = [
                SHOW_CACHED_TIP,
                LOAD_TIP,
                "Ask the host how much context",
                "Lock this model",
                "Release ",
                "Unload this model",
            ];
            if gateway.iter().any(|g| text.contains(g)) {
                rv.show(0);
            } else if models.iter().any(|g| text.contains(g)) {
                rv.show(1);
            } else if text.contains("Clear every prompt cache") {
                rv.show(2);
            }
        });
    }

    Element::new()
        .style(LayoutStyle::column().gap(0).grow(1.0).padding(Edges {
            left: 1,
            right: 1,
            top: 0,
            bottom: 0,
        }))
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
        // `m` pages the memory itemization (read MODULO the live window
        // count, so a bare `+ 1` is always valid and wraps to the top).
        .shortcut(KeyChord::plain(Key::Char('m')), move |_| {
            detail_top.update(|n| *n = n.saturating_add(1));
        })
        .shortcut(KeyChord::plain(Key::Char('a')), {
            let rv = reveal.clone();
            move |_| {
                let v = !show.get_untracked();
                set_show_cached(v);
                show.set(v);
                rv.show(1);
            }
        })
        .shortcut(KeyChord::plain(Key::Char('p')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "pause")
        })
        .shortcut(KeyChord::plain(Key::Char('L')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "login")
        })
        .shortcut(KeyChord::plain(Key::Char('U')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "check")
        })
        .shortcut(KeyChord::plain(Key::Char('I')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "update")
        })
        .shortcut(KeyChord::plain(Key::Char('R')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "restart")
        })
        .shortcut(KeyChord::plain(Key::Char('Q')), {
            let c = ctx.clone();
            move |_| gateway_action(cx, &c, "quit")
        })
        .child(head(cx, ctx, &tt))
        .child({
            // THE PAGE SCROLLS (DESIGN §3.11: "cards stack, the page
            // scrolls"): the Gateway card and the stacked cards below it.
            let content = Element::new()
                .style(LayoutStyle::column().gap(0).shrink(0.0))
                .child(gateway_card(cx, ctx, &tt, &reveal))
                .child(dyn_view_scoped(LayoutStyle::column().shrink(0.0), {
                    let ctx_body = ctx.clone();
                    let keeper = super::util::FocusKeeper::new();
                    let reveal = reveal.clone();
                    move |gcx| {
                        let data = store.host_state.get();
                        let ctx_b = ctx_body.clone();
                        let focus = focus.clone();
                        let reveal = reveal.clone();
                        super::util::loadable_view_kept(
                            &keeper,
                            &tt,
                            &store.conn.get(),
                            || store.tick.get(),
                            &data,
                            |_d: &HostStateData| false, // the strip renders even with zero rows
                            "",
                            |d| body(gcx, cx, &ctx_b, &tt, d, show, &focus, sels, &reveal),
                        )
                    }
                }))
                .build();
            Scroll::new(content)
                .axes(false, true)
                .offset_y(scroll)
                .layout(LayoutStyle::default().grow(1.0).basis(Dimension::Cells(0)))
                .scrollbar_auto_hide(true)
                .view(cx)
        })
        .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
            // The totals footer — pinned so the tables' growth never
            // squeezes it out.
            let text = store
                .host_state
                .with(|d| d.ready().map(totals_line))
                .unwrap_or_default();
            line(vec![span(text, tt.text_faint)])
        }))
        .build()
}

/// Title + subtitle, the Refresh button on the right.
fn head(pcx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let ctx = ctx.clone();
    let tt = *t;
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |hcx| {
        let w = page_w(hcx);
        let mut row = Element::new().style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .gap(1)
                .shrink(0.0),
        );
        let mut bw = 0;
        for a in head_actions() {
            bw += a.width() + 1;
            let c = ctx.clone();
            row = row.child(button(hcx, &tt, &a, On::Page, true, move || {
                c.refresh_screen(crate::ui::SCREEN_MODELS);
                super::host::refresh(&c);
            }));
        }
        let _ = pcx;
        if compact(hcx) {
            return Element::new()
                .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                .child(super::w::paint::fill_line(
                    LayoutStyle::default().grow(1.0).height(Dimension::Cells(1)),
                    vec![
                        super::w::Ink::new(TITLE, tt.text).bold(),
                        super::w::Ink::new(format!("  {SUBTITLE}"), tt.text_muted),
                    ],
                    None,
                ))
                .child(row.build())
                .build();
        }
        let title_w = abstracttui::text::width(SUBTITLE);
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

/// The ◎ Gateway card. Rebuilt only when its facts change (a memo over the
/// runner / tray / update / start-at-login answers): the runner poll must
/// not take the focus off its switches and buttons.
fn gateway_card(pcx: Scope, ctx: &Ctx, t: &TokenSet, reveal: &Reveal) -> View {
    let reveal_c = reveal.clone();
    let ctx = ctx.clone();
    let tt = *t;
    let store = ctx.store;
    let facts = pcx.memo(move || {
        let op = store.op;
        (
            store.conn.with(ConnPhase::is_connected),
            store.conn.with(ConnPhase::is_admin),
            op.runner.with(|r| r.ready().cloned()),
            op.runner.with(|r| match r {
                Loadable::Failed(e) => Some(e.to_string()),
                _ => None,
            }),
            op.tray.with(|r| r.ready().cloned()),
            op.update.with(|r| r.ready().cloned()),
            op.start_at_login.with(|r| r.ready().cloned()),
            op.lifecycle.get(),
        )
    });
    dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |gcx| {
        let (connected, admin, runner, runner_err, tray, update, login, lifecycle) = facts.get();
        let w = page_w(gcx);
        let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
        reveal_c.set_card(gateway_rows(&store, w, false) as i32);
        col = col.child(section_head(&tt, "◎", GATEWAY_TITLE));
        if w >= 100 {
            col = col.child(super::w::form::sentence(
                &tt,
                GATEWAY_NOTE,
                w,
                tt.text_muted,
            ));
        }
        if !connected {
            return col
                .child(line(vec![span(
                    "not connected — the host state needs a live gateway",
                    tt.warn,
                )]))
                .build();
        }
        // The state pill + the Workflows paused switch (admin).
        let mut state_row = Element::new().style(
            LayoutStyle::row()
                .height(Dimension::Cells(1))
                .gap(2)
                .shrink(0.0),
        );
        match (&runner, &runner_err) {
            (Some(r), _) => {
                let ink = if r.paused { tt.warn } else { tt.ok };
                state_row = state_row.child(super::w::paint::fill_line(
                    LayoutStyle::default()
                        .width(Dimension::Cells(
                            abstracttui::text::width(&r.state_text()) + 2,
                        ))
                        .height(Dimension::Cells(1))
                        .shrink(0.0),
                    vec![super::w::Ink::new(format!("● {}", r.state_text()), ink).bold()],
                    None,
                ));
                if admin {
                    let c = ctx.clone();
                    let tg = super::w::Toggle::new(r.paused)
                        .label(PAUSE_LABEL)
                        .tip(format!("{PAUSE_TIP}  (p)"))
                        .on_change(move |_| gateway_action(pcx, &c, "pause"));
                    state_row = state_row.child(tg.view(gcx, &tt));
                }
            }
            (None, Some(e)) => {
                state_row = state_row.child(line(vec![span(
                    format!("gateway state unavailable: {e}"),
                    tt.error,
                )]));
            }
            (None, None) => {
                state_row =
                    state_row.child(line(vec![span("◌ reading the gateway host…", tt.info)]));
            }
        }
        col = col.child(state_row.build());
        if let Some(r) = &runner {
            let d = r.detail_text();
            if !d.is_empty() {
                col = col.child(super::w::form::sentence(&tt, &d, w, tt.text_faint));
            }
        }
        let kv = |label: &str, value: View| -> View { super::util::field_w(&tt, label, 15, value) };
        if admin {
            if let Some(u) = &update {
                let mut text = u.version_text();
                let hint = u.hint_text();
                if !hint.is_empty() {
                    text.push_str(&format!(" · {hint}"));
                }
                col = col.child(kv(
                    "Version",
                    line(vec![span(
                        text,
                        if u.update_available {
                            tt.accent
                        } else {
                            tt.text
                        },
                    )]),
                ));
            }
        }
        if let Some(note) = &tray {
            col = col.child(kv("Desktop icon", line(vec![span(note.clone(), tt.text)])));
        }
        if let Some(h) = runner.as_ref().and_then(|r| r.last_hang.clone()) {
            // The web row + its tooltip ("Blocked N s in <frame>", "Every
            // thread's stack: …", "Incident file: …").
            let mut tip_lines = h.detail_lines();
            if let Some(dump) = h.dump_text() {
                tip_lines.insert(1, dump);
            }
            let row = Element::new()
                .style(LayoutStyle::line(1).shrink(0.0))
                .focusable()
                .child(line(vec![span(h.text(), tt.warn)]));
            let tipped = super::w::tip::with_tip(gcx, row, tip_lines.join("\n")).build();
            col = col.child(kv("Last restart", tipped));
            if let Some(dump) = h.dump_text() {
                col = col.child(kv("", line(vec![span(dump, tt.text_faint)])));
            }
        }
        if admin {
            let on = login.as_ref().map(|l| l.enabled).unwrap_or(false);
            let refused = match &login {
                None => Some("not read yet".to_string()),
                Some(l) if l.verb().is_none() => {
                    Some(format!("can't be changed here: {}", l.reason))
                }
                _ => None,
            };
            let c = ctx.clone();
            let tg = super::w::Toggle::new(on)
                .refused(refused)
                .tip("Start at login  (L)")
                .on_change(move |_| gateway_action(pcx, &c, "login"));
            let text = login
                .as_ref()
                .map(|l| l.text())
                .unwrap_or_else(|| "not read yet".to_string());
            col = col.child(kv(
                "Start at login",
                Element::new()
                    .style(
                        LayoutStyle::row()
                            .height(Dimension::Cells(1))
                            .gap(2)
                            .shrink(0.0),
                    )
                    .child(tg.view(gcx, &tt))
                    .child(line(vec![span(text, tt.text_muted)]))
                    .build(),
            ));
        }
        if let Some(l) = lifecycle {
            col = col.child(super::w::form::sentence(&tt, &l, w, tt.info));
        }
        let acts = gateway_actions(runner.as_ref(), update.as_ref(), admin);
        if !acts.is_empty() {
            let mut row = Element::new().style(
                LayoutStyle::row()
                    .height(Dimension::Cells(1))
                    .gap(1)
                    .shrink(0.0),
            );
            for a in acts {
                let c = ctx.clone();
                let id = a.id;
                row = row.child(button(gcx, &tt, &a, On::Page, true, move || {
                    gateway_action(pcx, &c, id)
                }));
            }
            col = col.child(row.build());
        }
        col.build()
    })
}

/// Rows the Gateway card takes (for the memory section's budget).
fn gateway_rows(store: &crate::store::Store, w: i32, compact: bool) -> usize {
    if compact {
        return 1;
    }
    let op = store.op;
    if !store.conn.with_untracked(ConnPhase::is_connected) {
        return 2;
    }
    let admin = store.conn.with_untracked(ConnPhase::is_admin);
    let runner = op.runner.with_untracked(|r| r.ready().cloned());
    let mut n = 2; // heading + state line
    if w >= 100 {
        n += super::util::wrap_text(GATEWAY_NOTE, w as usize).len();
    }
    if let Some(r) = &runner {
        if !r.detail_text().is_empty() {
            n += 1;
        }
        if let Some(h) = &r.last_hang {
            n += 1 + usize::from(h.dump_text().is_some());
        }
    }
    if admin && op.update.with_untracked(|u| u.ready().is_some()) {
        n += 1;
    }
    if op.tray.with_untracked(|t| t.ready().is_some()) {
        n += 1;
    }
    if admin {
        n += 2; // start at login + buttons
    }
    if op.lifecycle.with_untracked(Option::is_some) {
        n += 1;
    }
    n
}

/// A Gateway card action (a click, a switch or its key). Pause, Start at
/// login and Check now are the host panel's own verbs (ui/host.rs, F3).
/// Restart, Quit and Update stay here: this card says the WEB's sentences
/// and buttons (console.py restartGateway / quitGateway: "Restart" /
/// "Quit" + Cancel) and an update is a plain confirm (an install: the focus
/// on the action), where the F3 panel words them its own way.
fn gateway_action(cx: Scope, ctx: &Ctx, id: &str) {
    use crate::worker::operator::OpCmd;
    let connected = ctx.store.conn.with_untracked(ConnPhase::is_connected);
    let admin = ctx.store.conn.with_untracked(ConnPhase::is_admin);
    if let Some(why) = super::host::refusal(connected, admin) {
        ctx.store.notice.set(Some(why.into()));
        return;
    }
    let runner = ctx.store.op.runner.with_untracked(|r| r.ready().cloned());
    match id {
        // The host panel's own verbs (one implementation, F3 and here).
        "pause" => super::host::toggle_pause(ctx),
        "login" => super::host::toggle_start_at_login(cx, ctx, &|| {}),
        "check" => super::host::check_update(ctx),
        "update" => {
            let Some(u) = ctx.store.op.update.with_untracked(|u| u.ready().cloned()) else {
                ctx.store
                    .notice
                    .set(Some("check for an update first (Check now)".into()));
                return;
            };
            if let Some(why) = u.start_refusal() {
                ctx.store.notice.set(Some(why));
                return;
            }
            let c = ctx.clone();
            let sha = u.installer_sha256();
            super::w::Confirm::plain(u.confirm_text(), "Update now", "Not now").open(
                cx,
                ctx.ui,
                move || {
                    c.send(Cmd::Operator(OpCmd::UpdateStart {
                        installer_sha256: sha.clone(),
                    }))
                },
            );
        }
        "restart" | "quit" => {
            let restart = id == "restart";
            let a = gateway_actions(runner.as_ref(), None, true);
            if let Some(Err(why)) = a.iter().find(|x| x.id == id).map(|x| x.enabled.clone()) {
                ctx.store.notice.set(Some(format!(
                    "{} is not available: {why}",
                    if restart { "restart" } else { "quit" }
                )));
                return;
            }
            let c = ctx.clone();
            if restart {
                super::w::Confirm::danger(RESTART_QUESTION, "Restart", "Cancel").open(
                    cx,
                    ctx.ui,
                    move || c.send(Cmd::Operator(OpCmd::Restart)),
                );
            } else {
                super::w::Confirm::danger(QUIT_QUESTION, "Quit", "Cancel").open(
                    cx,
                    ctx.ui,
                    move || c.send(Cmd::Operator(OpCmd::Shutdown)),
                );
            }
        }
        _ => {}
    }
}

/// The Ready body, STACKED as on the web: ▦ Memory & GPU (the meters,
/// then the itemization — windowed on a short page, `m` pages it), ▣
/// Models (its head: the count, "Show configured / cached", [Load model];
/// the table; the selected row's facts) and ⌸ Session caches (its table).
/// The page scrolls (the caller's Scroll); a focused control off-screen
/// brings its section into view (`Reveal`).
#[allow(clippy::too_many_arguments)]
fn body(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    d: &HostStateData,
    show: Signal<bool>,
    focus: &TableFocus,
    sels: (Signal<Option<String>>, Signal<Option<String>>),
    reveal: &Reveal,
) -> View {
    let tt = *t;
    let ui = ctx.ui;
    let admin = ctx.store.conn.with(ConnPhase::is_admin);
    let all_models = d.models.clone();
    let viewport = crate::ui::page_viewport(cx).get();
    let wrap_w = (viewport.w as usize)
        .saturating_sub(BLOCK_CHROME as usize + 6)
        .max(24);
    let head = head_rows(t, d);
    let detail = detail_rows(t, d, wrap_w);
    let total = detail.len();
    // A short page windows the itemization (the cards below come first; it
    // is one `m` away); a tall one shows it whole.
    let win = if viewport.h < 30 { total.min(3) } else { total };
    let positions = total - win + 1;
    let shown_detail = win + usize::from(win < total);

    let mut strip = Element::new().style(LayoutStyle::column().gap(0).shrink(0.0));
    strip = strip.child(section_head(&tt, "▦", MEMORY_TITLE));
    let head_n = head.len();
    for row in head {
        strip = strip.child(row);
    }
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
            if win < total {
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

    // Where each card starts inside the scrolled content (the reveal map):
    // the Gateway card is above this body.
    let card = gateway_rows(&ctx.store, viewport.w - 2, false) as i32;
    let models_y = card + 1 + head_n as i32 + shown_detail as i32;
    let vis_n = visible_models(&all_models, show.get_untracked()).len() as i32;
    let models_h = 1 + 2 + 2 * vis_n.max(1) + 1;
    reveal.set_sections(
        models_y,
        models_h,
        models_y + models_h,
        3 + d.caches.len().max(1) as i32,
    );

    Element::new()
        .style(LayoutStyle::column().gap(0).shrink(0.0))
        .child(strip.build())
        .child(models_section(
            cx,
            pcx,
            ctx,
            &tt,
            &all_models,
            show,
            admin,
            focus,
            sels.0,
            reveal,
        ))
        .child(caches_section(
            cx, pcx, ctx, &tt, &d.caches, admin, focus, sels.1, reveal,
        ))
        .build()
}

/// Focus follows scroll (Network's `reveal`): the page scrolls only when the
/// focused control's section is off-screen — never under a click on a
/// visible control. Sections: 0 = the Gateway card, 1 = Models, 2 = caches.
#[derive(Clone)]
pub struct Reveal {
    scroll: Signal<i32>,
    visible: Signal<i32>,
    map: std::rc::Rc<std::cell::Cell<[(i32, i32); 3]>>,
}

impl Reveal {
    fn set_sections(&self, my: i32, mh: i32, cy: i32, ch: i32) {
        let mut m = self.map.get();
        m[1] = (my, mh);
        m[2] = (cy, ch);
        self.map.set(m);
    }
    fn set_card(&self, h: i32) {
        let mut m = self.map.get();
        m[0] = (0, h);
        self.map.set(m);
    }
    /// Bring section `i` (its top, and as much of it as fits) into view.
    pub fn show(&self, i: usize) {
        let (y, h) = self.map.get()[i];
        self.show_span(y, h);
    }
    /// Bring row `idx` of section `i`'s table into view (rows ≈ 2 lines;
    /// `head` = the lines above the table body inside the section).
    pub fn show_row(&self, i: usize, head: i32, idx: usize) {
        let (y, _) = self.map.get()[i];
        self.show_span(y + head + 2 * idx as i32, 2);
    }
    fn show_span(&self, y: i32, h: i32) {
        let top = self.scroll.get_untracked();
        let vis = self.visible.get_untracked().max(3);
        let want = if y < top {
            y
        } else if y + h.min(vis) > top + vis {
            (y + h.min(vis) - vis).max(0)
        } else {
            top
        };
        if want != top {
            self.scroll.set(want);
        }
    }
}

fn tone_ink(t: &TokenSet, tone: Tone) -> Rgba {
    match tone {
        Tone::Accent => t.accent,
        Tone::Ok => t.ok,
        Tone::Info => t.info,
        Tone::Warn => t.warn,
        Tone::Error => t.error,
        _ => t.text_muted,
    }
}

/// The Flags cell (the web's chips): locked, default, pinned.
pub fn flags_cell(r: &ModelRow) -> String {
    let mut out: Vec<&str> = Vec::new();
    if r.locked == Some(true) {
        out.push("⊘ locked");
    }
    if r.default == Some(true) {
        out.push("default");
    }
    if r.pinned == Some(true) {
        out.push("pinned");
    }
    out.join(" ")
}

/// ▣ Models: the card head — the title with its resident count, the
/// "Show configured / cached (N)" Toggle (always there, N = 0 too) and
/// [Load model] — then the table (resident rows; the toggle adds the rest)
/// and the selected row's facts.
#[allow(clippy::too_many_arguments)]
fn models_section(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    all: &[ModelRow],
    show: Signal<bool>,
    admin: bool,
    focus: &TableFocus,
    sel: Signal<Option<String>>,
    reveal: &Reveal,
) -> View {
    let tt = *t;
    let ui = ctx.ui;
    let cached_n = all.iter().filter(|r| r.resident != Some(true)).count();
    let title = format!("▣ {}", models_tab_title(all));
    let title_w = abstracttui::text::width(&title);
    let mut bar = Element::new().style(
        LayoutStyle::row()
            .height(Dimension::Cells(1))
            .gap(2)
            .shrink(0.0),
    );
    bar = bar.child(super::w::paint::fill_line(
        LayoutStyle::default()
            .width(Dimension::Cells(title_w))
            .height(Dimension::Cells(1))
            .shrink(0.0),
        vec![
            super::w::Ink::new("▣ ", tt.accent),
            super::w::Ink::new(models_tab_title(all), tt.text).bold(),
        ],
        None,
    ));
    {
        let rv = reveal.clone();
        let tg = super::w::Toggle::bound(show)
            .label(format!("Show configured / cached ({cached_n})"))
            .tip(format!("{SHOW_CACHED_TIP}  (a)"))
            .on_change(move |v| {
                set_show_cached(v);
                show.set(v);
                rv.show(1);
            });
        bar = bar.child(tg.view(cx, &tt));
    }
    for a in models_actions(admin) {
        let c = ctx.clone();
        bar = bar.child(button(cx, &tt, &a, On::Page, true, move || {
            open_warmup_form(pcx, &c)
        }));
    }
    // The size column's marker, said where the column is.
    bar = bar.child(line(vec![span(ESTIMATE_MARKER, tt.text_faint)]));
    let rows_src = all.to_vec();
    let models_detail = all.to_vec();
    let ctx2 = ctx.clone();
    let focus = focus.clone();
    let reveal_t = reveal.clone();
    let table = dyn_view_scoped(LayoutStyle::column().shrink(0.0), move |tcx| {
        let vis = visible_models(&rows_src, show.get());
        let w = (crate::ui::page_viewport(tcx).get().w - 4).max(20); // − the page scrollbar
        if vis.is_empty() {
            let text = if rows_src.is_empty() {
                MODELS_EMPTY.to_string()
            } else {
                format!(
                    "No models resident in memory right now — {cached_n} configured / cached row{} behind the toggle above.",
                    if cached_n == 1 { "" } else { "s" }
                )
            };
            let mut el = Element::new()
                .style(LayoutStyle::column().grow(1.0))
                .focusable();
            if focus.wants() {
                el = el.autofocus();
            }
            return focus.wrap(
                el.child(super::w::form::sentence(&tt, &text, w, tt.text_muted))
                    .build(),
            );
        }
        // The KV cache column joins where the row still fits on one line.
        let wide = w >= 140;
        let rows: Vec<WRow> = vis
            .iter()
            .enumerate()
            .map(|(i, r)| {
                let (tone, label) = task_tone(r.task.as_deref());
                let modality = Cell::Badge {
                    label: label.to_string(),
                    ink: tone_ink(&tt, tone),
                    action: None,
                    tip: r.task.clone(),
                };
                let mut cells = vec![
                    modality,
                    Cell::text(r.provider.clone().unwrap_or_else(|| "—".into()), tt.text),
                    Cell::text(r.model.clone().unwrap_or_else(|| "—".into()), tt.text),
                    Cell::text(
                        resident_label(r.resident),
                        if r.resident == Some(true) {
                            tt.ok
                        } else {
                            tt.text_muted
                        },
                    ),
                    Cell::text(size_cell(r), tt.text),
                ];
                if wide {
                    cells.push(Cell::text(cache_cell(r), tt.text_muted));
                }
                cells.push(Cell::text(ctx_cell(r), tt.text));
                cells.push(Cell::text(flags_cell(r), tt.text_muted));
                cells.push(Cell::Actions(row_actions(r, admin)));
                WRow::new(model_key(r, i), cells).dim(r.resident != Some(true))
            })
            .collect();
        let mut cols = vec![
            Col::new("Modality", ColW::Fit { min: 5, max: 10 }),
            Col::new("Provider", ColW::Fit { min: 6, max: 16 }),
            Col::new(
                "Model",
                ColW::Flex {
                    weight: 1,
                    min: if w >= 100 { 16 } else { 10 },
                },
            ),
            Col::new("Resident", ColW::Fit { min: 5, max: 10 }),
            Col::new("Size", ColW::Fit { min: 4, max: 10 }),
        ];
        if wide {
            cols.push(Col::new("Cache", ColW::Fit { min: 5, max: 10 }));
        }
        cols.push(Col::new("Context", ColW::Fit { min: 5, max: 8 }));
        cols.push(Col::new("Flags", ColW::Fit { min: 5, max: 18 }));
        cols.push(Col::new(
            "Actions",
            ColW::Fit {
                min: 8,
                max: if wide { 30 } else { 20 },
            },
        ));
        let c_a = ctx2.clone();
        let rv = reveal_t.clone();
        let mut dt = DataTable::new(cols, rows, sel)
            .width(w)
            .on_focus(move || rv.show(1))
            .on_action(move |key, id| model_action(pcx, &c_a, key, id, show.get_untracked()));
        if focus.wants() {
            dt = dt.autofocus();
        }
        focus.wrap(dt.view(tcx, &tt))
    });
    // The selected row's facts: the toned modality badge, the full
    // identifiers, the state facts that earn no column of their own.
    let detail = dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
        let t = tt;
        let vis = visible_models(&models_detail, show.get());
        let Some(r) = vis.get(ui.model_sel.get()) else {
            return line(vec![span(String::new(), t.text)]);
        };
        let (tone, label) = task_tone(r.task.as_deref());
        let mut bits: Vec<String> = Vec::new();
        bits.push(format!(
            "{} / {}",
            r.provider.as_deref().unwrap_or("—"),
            r.model.as_deref().unwrap_or("—")
        ));
        bits.push(match lock_action(r) {
            LockAction::Unlock => "locked".to_string(),
            LockAction::Lock { adopt: true } => "lockable (adopts it)".to_string(),
            LockAction::Lock { adopt: false } => "lockable".to_string(),
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
        Element::new()
            .style(LayoutStyle::row().gap(1).h(1).shrink(0.0))
            .child(badge(&t, label, tone))
            .child(line(vec![span(bits.join("  ·  "), t.text_muted)]))
            .build()
    });
    Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(bar.build())
        // The selected row's facts sit under the card head, so they are on
        // screen whenever the card is (the table may be taller than the page).
        .child(detail)
        .child(table)
        .build()
}

/// The web's `_fmtEpochS`: UTC "YYYY-MM-DD HH:MM:SS", empty when unknown
/// (never a fabricated 0).
pub fn fmt_epoch_s(s: Option<f64>) -> String {
    let Some(s) = s.filter(|s| s.is_finite() && *s > 0.0) else {
        return String::new();
    };
    let secs = s.floor() as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// ⌸ Session caches: the card head, then Session · Model · Size · Tokens ·
/// Created · Actions (the web's table; Created empty when unknown).
#[allow(clippy::too_many_arguments)]
fn caches_section(
    cx: Scope,
    pcx: Scope,
    ctx: &Ctx,
    t: &TokenSet,
    caches: &[SessionCacheRow],
    admin: bool,
    focus: &TableFocus,
    sel: Signal<Option<String>>,
    reveal: &Reveal,
) -> View {
    let tt = *t;
    let _ = focus;
    let w = (crate::ui::page_viewport(cx).get().w - 4).max(20); // − the page scrollbar
    let mut col = Element::new()
        .style(LayoutStyle::column().shrink(0.0))
        .child(section_head(&tt, "⌸", CACHES_TITLE));
    if caches.is_empty() {
        return col
            .child(super::w::form::sentence(
                &tt,
                CACHES_EMPTY,
                w,
                tt.text_muted,
            ))
            .build();
    }
    let rows: Vec<WRow> = caches
        .iter()
        .map(|c| {
            let model = [c.provider.as_str(), c.model.as_str()]
                .iter()
                .filter(|x| !x.is_empty())
                .cloned()
                .collect::<Vec<_>>()
                .join("/");
            WRow::new(
                c.key.clone(),
                vec![
                    Cell::text(c.session_id.clone(), tt.text),
                    Cell::text(model, tt.text),
                    Cell::text(c.bytes.map(human_bytes).unwrap_or_default(), tt.text),
                    Cell::text(
                        c.token_count.map(|n| n.to_string()).unwrap_or_default(),
                        tt.text,
                    ),
                    Cell::text(fmt_epoch_s(c.created_at_s), tt.text_muted),
                    Cell::Actions(cache_actions(c, admin)),
                ],
            )
        })
        .collect();
    let cols = vec![
        Col::new("Session", ColW::Flex { weight: 1, min: 10 }),
        Col::new("Model", ColW::Flex { weight: 1, min: 10 }),
        Col::new("Size", ColW::Fit { min: 4, max: 10 }),
        Col::new("Tokens", ColW::Fit { min: 6, max: 9 }),
        Col::new("Created", ColW::Fit { min: 7, max: 19 }),
        Col::new("Actions", ColW::Fit { min: 8, max: 8 }),
    ];
    let c_a = ctx.clone();
    let rv = reveal.clone();
    let dt = DataTable::new(cols, rows, sel)
        .width(w)
        .on_focus(move || rv.show(2))
        .on_action(move |key, id| cache_action(pcx, &c_a, key, id));
    col = col.child(dt.view(cx, &tt));
    col.build()
}

/// A model row action (a click): the row becomes the selection, the verb
/// runs (the same bodies as the keys).
fn model_action(cx: Scope, ctx: &Ctx, key: &str, id: &str, show: bool) {
    let keys: Vec<String> = ctx.store.host_state.with_untracked(|d| {
        d.ready()
            .map(|d| {
                visible_models(&d.models, show)
                    .iter()
                    .enumerate()
                    .map(|(i, r)| model_key(r, i))
                    .collect()
            })
            .unwrap_or_default()
    });
    if let Some(i) = keys.iter().position(|k| k == key) {
        ctx.ui.model_sel.set(i);
    }
    match id {
        "estimate" => estimate_selected(ctx),
        "lock" | "unlock" => toggle_lock_selected(cx, ctx),
        "unload" => unload_selected(cx, ctx),
        _ => {}
    }
}

/// A cache row action (a click): select it, then clear.
fn cache_action(cx: Scope, ctx: &Ctx, key: &str, id: &str) {
    let i = ctx.store.host_state.with_untracked(|d| {
        d.ready()
            .and_then(|d| d.caches.iter().position(|c| c.key == key))
    });
    if let Some(i) = i {
        ctx.ui.cache_sel.set(i);
    }
    if id == "clear" {
        clear_caches_selected(cx, ctx);
    }
}

/// The page's hint pairs (R15: the selected row's actions, then the page's).
pub fn hints(ctx: &Ctx) -> Vec<(&'static str, &'static str)> {
    let store = ctx.store;
    let _ = (ctx.ui.model_sel.get(), ctx.ui.cache_sel.get());
    let mut out = vec![("↑↓", "rows"), ("Tab", "actions"), ("e", "Estimate")];
    // The residency verbs (admin: folded into "u/k/w/c admin only" for
    // everyone else by the footer, `ADMIN_KEYS`).
    out.push(("u", "Unload"));
    out.push(("k", "Lock/Unlock"));
    out.push(("w", "Load model"));
    out.push(("c", "Clear session caches"));
    out.push(("a", "Show configured / cached"));
    out.push(("m", "more memory detail"));
    if store.conn.with(ConnPhase::is_admin) {
        out.push(("p", "Workflows paused"));
        out.push(("L", "Start at login"));
        out.push(("U", "Check now"));
        out.push(("R", "Restart gateway…"));
        out.push(("Q", "Quit gateway…"));
    }
    out.push(("r", "Refresh"));
    out
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
        let frac = ram.percent.map(|p| (p / 100.0) as f32).or_else(|| {
            match (ram.used_bytes, ram.total_bytes) {
                (Some(u), Some(total)) if total > 0 => Some(u as f32 / total as f32),
                _ => None,
            }
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

/// The web's section title: "Models (N resident)" — resident rows only.
pub fn models_tab_title(rows: &[ModelRow]) -> String {
    let n = rows.iter().filter(|r| r.resident == Some(true)).count();
    format!("Models ({n} resident)")
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

/// The selected row of the Models table (an index into the VISIBLE rows:
/// resident ones, plus the configured / cached ones behind the toggle).
fn selected_model(ctx: &Ctx) -> Option<ModelRow> {
    let idx = ctx.ui.model_sel.get_untracked();
    ctx.store.host_state.with_untracked(|d| {
        d.ready()
            .and_then(|d| visible_models(&d.models, show_cached()).get(idx).cloned())
    })
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
    if !super::util::admin_gate(&ctx.store, "unloading a model") {
        return;
    }
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
    super::w::Confirm::danger(
        format!("{}{locked_hint}", unload_question(&format!("{p}/{m}"))),
        "Unload",
        "Cancel",
    )
    .open(cx, ctx.ui, move || {
        send_mutation(&ctx2, format!("unloading {p}/{m}"), |op| Cmd::UnloadModel {
            provider: p,
            model: m,
            force: false,
            op,
        });
    });
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
    if !super::util::admin_gate(&ctx.store, "locking or unlocking a model") {
        return;
    }
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
            super::w::Confirm::danger(
                format!("Unlock {p}/{m}? An unlocked model can be evicted or unloaded."),
                "Unlock",
                "Cancel",
            )
            .open(cx, ctx.ui, move || {
                send_mutation(&ctx2, format!("unlocking {p}/{m}"), |op| Cmd::LockModel {
                    provider: p,
                    model: m,
                    lock: false,
                    op,
                });
            });
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
    if !super::util::admin_gate(&ctx.store, "loading (warming up) a model") {
        return;
    }
    let prefill = selected_model(ctx).and_then(|r| model_pair(&r));
    let store = ctx.store;
    // The catalog may never have been fetched (this tab is reachable
    // without visiting Providers) — ask for it, honestly, at open.
    if matches!(store.providers.get_untracked(), Loadable::NotAsked) {
        store.providers.set(Loadable::Loading);
        ctx.send(Cmd::LoadProviders);
    }
    let ctx2 = ctx.clone();
    super::w::FormModal::new(LOAD_TITLE)
        .lead(LOAD_TIP)
        .size(78, 18)
        .open(ctx, cx, move |mcx, close, _guard, _w| {
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
            let model_custom =
                mcx.signal(prefill.as_ref().map(|(_, m)| m.clone()).unwrap_or_default());
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
                .child(line(vec![span(
                    "the gateway pulls the model into host memory — a cold load can take a while",
                    t0.text_faint,
                )]))
                .child(field(
                    &t0,
                    "Provider",
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
                            "Model",
                            line(vec![span("choose a provider first", t.text_faint)]),
                        );
                    }
                    if ix >= custom_row {
                        return field(
                            &t,
                            "Model",
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
                                "Model",
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
                            "Model",
                            line(vec![span("⟳ discovering models…", t.info)]),
                        ),
                        // Discovery FAILED ≠ "this provider has no models":
                        // name the error and keep the free-text lane.
                        Loadable::Failed(e) => Element::new()
                            .style(LayoutStyle::column())
                            .child(field(
                                &t,
                                "Model",
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
                            "Model",
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
                    super::w::Toggle::switch(LOCK_IN_MEMORY, lock_after)
                        .tip(LOCK_IN_MEMORY_TIP)
                        .on_change(move |v| lock_after.set(v))
                        .view(mcx, &t0),
                ))
                .child(dyn_view(
                    LayoutStyle::line(1).shrink(0.0),
                    move || match form_error.get() {
                        Some(e) => line(vec![span_bold(format!("✗ {e}"), t0.error)]),
                        None => line(vec![span(String::new(), t0.text)]),
                    },
                ))
                .child(super::w::form::button_row(vec![
                    button(
                        mcx,
                        &t0,
                        &Action::label("cancel", "Cancel"),
                        On::Raised,
                        true,
                        move || close_cancel(),
                    ),
                    button(
                        mcx,
                        &t0,
                        &Action::label("load", "Load model").tooltip(LOAD_TIP),
                        On::Raised,
                        true,
                        move || {
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
                                    "pick a provider and a model — both name the target".into(),
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
                        },
                    ),
                ]))
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
    if !super::util::admin_gate(&ctx.store, "clearing session caches") {
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
    super::w::Confirm::danger(clear_cache_question(&sid), "Clear", "Cancel").open(
        cx,
        ctx.ui,
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
        assert_eq!(
            cells[7], "",
            "lockable:true is not LOCKED — the cell stays blank"
        );
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
            created_at_s: None,
        };
        assert_eq!(
            cache_row_cells(&r),
            vec![
                "agw.pc.v1.s-sess1:session",
                "sess1",
                "qwen",
                "4.0 KiB",
                "100"
            ]
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
        d.models
            .push(row(serde_json::json!({"provider": "p", "model": "m",
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
        d.models
            .push(row(serde_json::json!({"provider": "p", "model": "m2",
                                             "resident": true})));
        d.recount();
        assert_eq!(
            totals_line(&d),
            "totals: 1 resident / 2 model row(s) · 1.0 KiB · 1 session cache(s) · 2.0 KiB"
        );
    }
}
