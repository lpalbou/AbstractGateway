//! Shared UI components — plain functions over the token set.
//!
//! The engine's component pattern: `fn(t: &TokenSet, props…) -> View`.
//! Everything consumes theme tokens; no raw colors anywhere.

use abstracttui::base::{Point, Rgba};
use abstracttui::prelude::*;
use abstracttui::render::Style;
use abstracttui::widgets::{Badge, Tone};

use crate::api::{ApiError, ApiErrorKind};
use crate::store::Loadable;

/// KEYBOARD OWNERSHIP ACROSS RE-RENDERS. A screen's primary widget (its
/// table) lives inside a reactive region that regenerates when data lands
/// (a poll, a lazy read): the old instance unmounts WITH the focus, the
/// engine drops focus to nothing, and the screen's keys — which resolve
/// along the root→focus path — go dead. A first-mount-only autofocus
/// cannot fix it, and an always-on one steals the keyboard back from
/// whatever the user moved to.
///
/// The keeper remembers whether its widget held the keyboard when it
/// vanished (an unmount delivers no FocusOut, a Tab/click away does) and
/// hands the focus to the next instance only then — or while the page's
/// widget never got the keyboard at all (a first autofocus can be
/// dropped when its instance is replaced in the same frame, e.g. the
/// loading anchor giving way to the table). Create one per page
/// (`FocusKeeper::new()` in the page scope, outside the region) and pass
/// every instance of the primary widget through [`FocusKeeper::wire`];
/// states with no widget (empty, loading, error) go through
/// [`FocusKeeper::anchor`] so the keys stay live there too.
#[derive(Clone, Default)]
pub struct FocusKeeper {
    /// The current (or last unmounted) instance holds the keyboard.
    held: std::rc::Rc<std::cell::Cell<bool>>,
    /// Some instance ever received the keyboard on this page.
    ever: std::rc::Rc<std::cell::Cell<bool>>,
}

impl FocusKeeper {
    pub fn new() -> FocusKeeper {
        FocusKeeper::default()
    }

    /// Whether the next instance takes the focus.
    fn wants_focus(&self) -> bool {
        !self.ever.get() || self.held.get()
    }

    /// Wire one instance of the primary widget.
    pub fn wire(&self, el: Element) -> View {
        let want = self.wants_focus();
        let (held, ever) = (self.held.clone(), self.ever.clone());
        let el = el.on(abstracttui::ui::Phase::Bubble, move |_ctx, ev| match ev {
            abstracttui::ui::UiEvent::FocusIn => {
                held.set(true);
                ever.set(true);
            }
            abstracttui::ui::UiEvent::FocusOut => held.set(false),
            _ => {}
        });
        if want {
            el.autofocus().build()
        } else {
            el.build()
        }
    }

    /// A focusable stand-in for a widget-less state (the screen's keys
    /// stay live on an empty list or while loading).
    pub fn anchor(&self, content: View) -> View {
        self.wire(
            Element::new()
                .focusable()
                .style(LayoutStyle::column().grow(1.0))
                .child(content),
        )
    }
}

/// A styled span for [`line`].
pub type SpanSpec = (String, Rgba, bool);

pub fn span(text: impl Into<String>, ink: Rgba) -> SpanSpec {
    (text.into(), ink, false)
}

pub fn span_bold(text: impl Into<String>, ink: Rgba) -> SpanSpec {
    (text.into(), ink, true)
}

/// One row of styled spans (hand-rolled multi-ink line; `Feed` rich
/// lines are transcript-shaped, this is chrome-shaped).
pub fn line(spans: Vec<SpanSpec>) -> View {
    line_styled(LayoutStyle::line(1), spans)
}

pub fn line_styled(style: LayoutStyle, spans: Vec<SpanSpec>) -> View {
    Element::new()
        .style(style)
        .draw(move |canvas, rect| {
            // R4 (engine wave10 recipe; letter P1.3): a draw closure owns
            // its rect's INTERIOR — a partially crushed rect still reaches
            // us with less than we asked for, and the closure clipped on
            // one axis only (the exact 1030 class).
            if rect.is_empty() {
                return;
            }
            let mut x = rect.x;
            let right = rect.x + rect.w;
            for (text, ink, bold) in &spans {
                if x >= right {
                    break;
                }
                let mut st = Style::new().fg(*ink);
                if *bold {
                    st = st.bold();
                }
                // Clip to OUR rect: draw closures paint into the damage
                // region, not the element box — an unclipped long error
                // string would run across the Block border (engine
                // clips only what the damage rect happens to cut).
                let budget = (right - x).max(0) as usize;
                let fitted = fit_width(text, budget);
                canvas.print_styled(Point::new(x, rect.y), &fitted, &st);
                x += abstracttui::text::width(&fitted);
            }
        })
        .build()
}

/// Cell-width-aware truncation with an honest `…` marker.
fn fit_width(text: &str, max_cells: usize) -> String {
    let max = max_cells as i32;
    if abstracttui::text::width(text) <= max {
        return text.to_string();
    }
    let mut out = String::new();
    let mut used = 0i32;
    let budget = max.saturating_sub(1); // room for the marker
    for ch in text.chars() {
        let w = abstracttui::text::width(&ch.to_string());
        if used + w > budget {
            break;
        }
        out.push(ch);
        used += w;
    }
    out.push('…');
    out
}

/// Cell-width word wrap for prose that must be READABLE IN FULL —
/// `line()` truncates, and a paid-for sandbox response or a spec-exact
/// explanatory note may be neither cut nor reworded. Respects explicit
/// newlines; words longer than the width hard-break.
pub fn wrap_text(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1) as i32;
    let mut out = Vec::new();
    for raw in text.split('\n') {
        let mut cur = String::new();
        let mut cur_w = 0i32;
        for word in raw.split(' ') {
            let ww = abstracttui::text::width(word);
            let sep = if cur.is_empty() { 0 } else { 1 };
            if cur_w + sep + ww <= width {
                if sep == 1 {
                    cur.push(' ');
                }
                cur.push_str(word);
                cur_w += sep + ww;
                continue;
            }
            if !cur.is_empty() {
                out.push(std::mem::take(&mut cur));
            }
            if ww <= width {
                cur.push_str(word);
                cur_w = ww;
            } else {
                // Hard-break an over-wide word cell-accurately.
                let mut piece = String::new();
                let mut pw = 0i32;
                for ch in word.chars() {
                    let cw = abstracttui::text::width(&ch.to_string());
                    if pw + cw > width && !piece.is_empty() {
                        out.push(std::mem::take(&mut piece));
                        pw = 0;
                    }
                    piece.push(ch);
                    pw += cw;
                }
                cur = piece;
                cur_w = pw;
            }
        }
        out.push(cur);
    }
    out
}
/// A labeled form row: fixed-width muted label, any child beside it.
pub fn field(t: &TokenSet, label: &str, child: View) -> View {
    field_w(t, label, 18, child)
}

pub fn field_w(t: &TokenSet, label: &str, label_w: i32, child: View) -> View {
    let ink = t.text_muted;
    let label = label.to_string();
    Element::new()
        .style(LayoutStyle::row().gap(1))
        .child(
            Element::new()
                .style(LayoutStyle::default().w(label_w).h(1).shrink(0.0))
                .draw(move |canvas, rect| {
                    if rect.is_empty() {
                        return; // R4: same interior-ownership rule as line_styled
                    }
                    let fitted = fit_width(&label, rect.w.max(0) as usize);
                    canvas.print(Point::new(rect.x, rect.y), &fitted, ink, Rgba::TRANSPARENT);
                })
                .build(),
        )
        .child(child)
        .build()
}

/// The notice Esc leaves when it hands the keyboard back to the screen.
pub const FOCUS_RELEASED: &str =
    "text field released — the screen's keys work again (Esc again goes back)";

/// Esc on a PAGE-level text field (never a modal's: there Esc closes the
/// modal) releases the keyboard to the screen instead of navigating:
/// a text field swallows every plain key (digits, verbs, `q`), so without
/// this a caret parked in a field — the URL box at boot, the sandbox
/// prompt — turns every screen key into typing with no visible way out
/// but Tab. The field's own shortcut is the deepest registration, so it
/// wins over the root's Esc (back); the SECOND Esc, with nothing focused,
/// reaches the root and goes back as before.
///
/// Mechanics: a shortcut runs after the handler walk, whose last bubble
/// step is the tree root — `current()` is that root, a non-focusable
/// element. Focusing it is the engine's blur (key dispatch targets
/// `focus.or(root)`, so the root's shortcuts own the keyboard again).
pub fn esc_releases_focus(el: Element, notice: Signal<Option<String>>) -> Element {
    el.shortcut(KeyChord::plain(Key::Escape), move |ecx| {
        if let Some(root) = ecx.current() {
            ecx.request_focus(root);
            notice.set(Some(FOCUS_RELEASED.into()));
        }
    })
}

/// Clamp a table-selection signal when its row count shrinks (deleting
/// the last row otherwise leaves the highlight past the end: no row
/// selected, every row action silently dead). Call from the screen's
/// scope with a closure reading the domain signal (tracked).
pub fn clamp_selection(
    cx: abstracttui::reactive::Scope,
    sel: abstracttui::reactive::Signal<usize>,
    len_of: impl Fn() -> usize + 'static,
) {
    cx.effect(move || {
        let len = len_of();
        let cur = sel.get();
        if len == 0 {
            if cur != 0 {
                sel.set(0);
            }
        } else if cur >= len {
            sel.set(len - 1);
        }
    });
}

/// The ONE admin gate for a verb (key press, menu pick, form save): true
/// when the caller may send it; otherwise the notice lane says why
/// ([`crate::store::ConnPhase::admin_refusal`]) and nothing is sent.
pub fn admin_gate(store: &crate::store::Store, what: &str) -> bool {
    match store.conn.with_untracked(|c| c.admin_refusal(what)) {
        Some(why) => {
            store.notice.set(Some(why));
            false
        }
        None => true,
    }
}

/// Footer pairs for a screen. For a principal known NOT to be an admin the
/// verbs in `admin_keys` leave their slots and come back as ONE compact
/// disabled-with-the-reason pair — `("w/a/D/C", "admin only")`, the
/// returned key string — the way the shared Models/Engines screens label
/// theirs ("download: admin only"). Pressing one still answers with the
/// full reason.
pub fn admin_hint_pairs<'a>(
    pairs: Vec<(&'a str, &'a str)>,
    admin_keys: &[&str],
    non_admin: bool,
) -> (Vec<(&'a str, &'a str)>, Option<String>) {
    if !non_admin {
        return (pairs, None);
    }
    let (gated, kept): (Vec<_>, Vec<_>) =
        pairs.into_iter().partition(|(k, _)| admin_keys.contains(k));
    let keys = gated.iter().map(|(k, _)| *k).collect::<Vec<_>>().join("/");
    (kept, (!keys.is_empty()).then_some(keys))
}

/// Status badge for configured / covered / default / error states.
pub fn badge(t: &TokenSet, label: &str, tone: Tone) -> View {
    Badge::new(label).tone(tone).element(t).build()
}

/// The honest-state panel around remote data: distinct renders for
/// not-asked / loading / failed(kind) / ready-but-empty.
///
/// `tick` is LAZY (a closure, called only by the Loading arm): the
/// busy heartbeat must be a tracked dependency of the caller's
/// dyn_view ONLY while a spinner is actually showing. A value param
/// forced every call site to read it unconditionally, so every Ready
/// table's region regenerated twice a second during ANY in-flight op
/// — and each regeneration re-mounted the table's `.autofocus()`,
/// yanking focus off whatever the operator had reached (on the
/// Runtimes screen the next arrow key then CHOSE a plane — a load
/// nobody asked for).
pub fn loadable_view<T>(
    t: &TokenSet,
    conn: &crate::store::ConnPhase,
    tick: impl Fn() -> u64,
    data: &Loadable<T>,
    empty_check: impl Fn(&T) -> bool,
    empty_text: &str,
    ready: impl FnOnce(&T) -> View,
) -> View {
    match data {
        Loadable::NotAsked => line(vec![span(
            "— not loaded yet (connect first, or press r to refresh)",
            t.text_muted,
        )]),
        // ANIMATED spinner (operator ask 2026-07-26: "if there's some
        // loading, we should have some sort of spinwheel"). The frame
        // clock is store.tick — the worker's busy heartbeat, read
        // TRACKED (ambient tracking reaches through this call) inside
        // the Loading arm only, so the region re-renders (and the
        // glyph turns) exactly while an op is in flight AND a spinner
        // is on screen. Slow loads (an entity plane open can take
        // seconds) visibly spin instead of freezing on a static glyph.
        Loadable::Loading => abstracttui::widgets::Spinner::new()
            .frame(tick())
            .label("loading…")
            .element(t)
            .build(),
        // The failure story defers to the centralized connection
        // authority (round-4): verifying / settled-down / this-endpoint.
        Loadable::Failed(e) => error_panel_conn(t, e, conn, None),
        Loadable::Ready(v) if empty_check(v) => {
            line(vec![span(format!("∅ {empty_text}"), t.text_muted)])
        }
        Loadable::Ready(v) => ready(v),
    }
}

/// [`loadable_view`] for a region whose ready state is a screen's primary
/// widget wired through `keeper`: every other state (not asked, loading,
/// failed, empty) renders on the keeper's focusable anchor, so the
/// screen's keys stay live while the data is away.
#[allow(clippy::too_many_arguments)]
pub fn loadable_view_kept<T>(
    keeper: &FocusKeeper,
    t: &TokenSet,
    conn: &crate::store::ConnPhase,
    tick: impl Fn() -> u64,
    data: &Loadable<T>,
    empty_check: impl Fn(&T) -> bool,
    empty_text: &str,
    ready: impl FnOnce(&T) -> View,
) -> View {
    let widget = matches!(data, Loadable::Ready(v) if !empty_check(v));
    let view = loadable_view(t, conn, tick, data, &empty_check, empty_text, ready);
    if widget {
        view
    } else {
        keeper.anchor(view)
    }
}

/// Failure rendering that keeps the error KINDS distinct (unreachable ≠
/// unauthorized ≠ forbidden ≠ HTTP detail) — the honest-states law.
pub fn error_panel(t: &TokenSet, e: &ApiError) -> View {
    error_panel_hint(t, e, None)
}

/// Connection-aware variant (round-4 architecture review): the
/// UNREACHABLE arm's story defers to the centralized authority —
/// while it verifies, the panel says so (info tone, not a
/// final-sounding error); when the connection settled DOWN, every
/// panel tells the ONE story; when it checks out Connected, the panel
/// persists only after its automatic retry already failed, so the
/// "this endpoint, not the gateway" copy is earned. Every other kind
/// is a domain truth and renders unchanged.
pub fn error_panel_conn(
    t: &TokenSet,
    e: &ApiError,
    conn: &crate::store::ConnPhase,
    retry_hint: Option<&str>,
) -> View {
    use crate::store::ConnPhase;
    if e.kind == ApiErrorKind::Unreachable {
        match conn {
            ConnPhase::Verifying(_) => {
                return Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![span_bold(
                        "◌ network hiccup — verifying the gateway connection…",
                        t.info,
                    )]))
                    .child(line(vec![span(format!("  {}", e.message), t.text_muted)]))
                    .build();
            }
            ConnPhase::Connected(_) => {
                let hint = retry_hint
                    .unwrap_or("press r to retry — the connection checks out, so this endpoint is the problem; check the gateway logs");
                return Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![span_bold(
                        "✗ request failed (network) — the gateway itself answers",
                        t.error,
                    )]))
                    .child(line(vec![span(format!("  {}", e.message), t.text)]))
                    .child(line(vec![span(format!("  {hint}"), t.text_muted)]))
                    .build();
            }
            ConnPhase::NotConnected
            | ConnPhase::Probing
            | ConnPhase::Unauthorized(_)
            | ConnPhase::Forbidden(_)
            | ConnPhase::NotGateway(_, _)
            | ConnPhase::Unreachable(_) => {
                return Element::new()
                    .style(LayoutStyle::column())
                    .child(line(vec![span_bold("✗ gateway connection lost", t.error)]))
                    .child(line(vec![span(format!("  {}", e.message), t.text)]))
                    .child(line(vec![span(
                        "  fix it on the Connection screen — every screen tells this same story",
                        t.text_muted,
                    )]))
                    .build();
            }
        }
    }
    error_panel_hint(t, e, retry_hint)
}

/// `error_panel` with a surface-appropriate RETRY teaching (round-4
/// transport audit: the default hint teaches `r`, which is DEAD inside
/// modals — the root shortcut doesn't fire there and refresh_screen
/// doesn't reload modal-scoped slots). The override replaces the hint
/// only for the transient classes (network / HTTP); auth hints stay —
/// they name the actual cause, which no retry wording improves.
pub fn error_panel_hint(t: &TokenSet, e: &ApiError, retry_hint: Option<&str>) -> View {
    let (head, hint) = match e.kind {
        // Honest scope (operator incident 2026-07-25): ONE request
        // failing at the network layer proves nothing about the
        // gateway — the header can rightly say connected while a
        // transient (dead pooled socket, server bounce mid-request)
        // kills a single read. Name the failed REQUEST, teach the
        // retry, and only point at connection-level causes as the
        // everything-fails escalation.
        ApiErrorKind::NotConnected => (
            "— not connected".to_string(),
            "probe on the Connection screen first".to_string(),
        ),
        ApiErrorKind::Unreachable => (
            "✗ request failed (network)".to_string(),
            "press r to retry — if every screen fails like this, check the URL on the Connection screen".to_string(),
        ),
        ApiErrorKind::Unauthorized => (
            "✗ unauthorized (401)".to_string(),
            "the token is wrong or missing — fix it on the Connection screen".to_string(),
        ),
        ApiErrorKind::Forbidden => (
            "✗ forbidden (403)".to_string(),
            "this needs an admin token".to_string(),
        ),
        ApiErrorKind::Http(code) => (format!("✗ HTTP {code}"), String::new()),
        ApiErrorKind::Protocol => (
            "✗ unexpected response".to_string(),
            "the gateway answered, but not with the expected JSON".to_string(),
        ),
    };
    let hint = match (&e.kind, retry_hint) {
        (ApiErrorKind::Unreachable | ApiErrorKind::Http(_), Some(h)) => h.to_string(),
        _ => hint,
    };
    let msg = e.message.clone();
    Element::new()
        .style(LayoutStyle::column())
        .child(line(vec![span_bold(head, t.error)]))
        .child(line(vec![span(format!("  {msg}"), t.text)]))
        .child(if hint.is_empty() {
            Element::new().style(LayoutStyle::default().h(0)).build()
        } else {
            line(vec![span(format!("  {hint}"), t.text_muted)])
        })
        .build()
}

/// Key-hint row for the footer: pairs of (key, action).
pub fn hints(t: &TokenSet, pairs: &[(&str, &str)]) -> View {
    let mut spans = Vec::new();
    for (i, (k, v)) in pairs.iter().enumerate() {
        if i > 0 {
            // One cell of air each side: at 80 columns the wider
            // separator alone cost a whole screen verb.
            spans.push(span(" · ", t.text_faint));
        }
        spans.push(span_bold((*k).to_string(), t.accent));
        spans.push(span(format!(" {v}"), t.text_muted));
    }
    line(spans)
}

/// `value` or an honest em-dash when absent.
pub fn or_dash(v: &Option<String>) -> String {
    match v {
        Some(s) if !s.is_empty() => s.clone(),
        _ => "—".into(),
    }
}

/// Truncate for table cells (display only, marked with …).
pub fn ellipsize(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let cut: String = s.chars().take(max.saturating_sub(1)).collect();
    format!("{cut}…")
}
