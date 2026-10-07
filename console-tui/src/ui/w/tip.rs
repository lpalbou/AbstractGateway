//! Tooltips on hover AND on keyboard focus (DESIGN-TUI.md §4.1, A2 — the
//! R9 kit rule "tooltip on keyboard focus"). Hover shows after a short
//! delay; focus shows at once and also writes the focused-control line
//! the status bar leads with. A passive draw layer above everything,
//! placed under the anchor (above when the bottom is short), removed on
//! leave / blur / anchor loss.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use abstracttui::app::{current_theme, current_viewport, LayerHandle, Overlays};
use abstracttui::base::{Point, Rect};
use abstracttui::prelude::*;
use abstracttui::reactive::after;
use abstracttui::render::Style;
use abstracttui::ui::{Phase, UiEvent};

/// The hover delay (the engine Tooltip's order of magnitude).
pub const HOVER_DELAY: Duration = Duration::from_millis(350);

thread_local! {
    /// The status bar's "focused control" line (installed by the shell).
    static FOCUS_LINE: RefCell<Option<Signal<Option<String>>>> = const { RefCell::new(None) };
    /// The overlay store tooltips draw on (installed by the shell).
    static OVERLAYS: RefCell<Option<Overlays>> = const { RefCell::new(None) };
}

/// Install the shell's focus line + overlay store (once, at root mount;
/// a test harness mounting `ui::root` gets it the same way).
pub fn install(focus_line: Signal<Option<String>>, overlays: Overlays) {
    FOCUS_LINE.with(|f| *f.borrow_mut() = Some(focus_line));
    OVERLAYS.with(|o| *o.borrow_mut() = Some(overlays));
}

/// The current focused-control line (the status bar reads it).
pub fn focus_line() -> Option<Signal<Option<String>>> {
    // A disposed root (a finished test, a remount) leaves a dead handle.
    FOCUS_LINE.with(|f| *f.borrow()).filter(|s| s.is_alive())
}

struct TipState {
    gen: u64,
    layer: Option<LayerHandle>,
    focused: bool,
}

impl TipState {
    fn hide(&mut self) {
        if let Some(l) = self.layer.take() {
            l.remove();
        }
    }
}

/// Attach a tooltip `text` to `el` (shown on hover after [`HOVER_DELAY`],
/// and at once on keyboard focus — which also names the control in the
/// status bar). Empty text attaches nothing.
pub fn with_tip(cx: Scope, el: Element, text: String) -> Element {
    if text.is_empty() {
        return el;
    }
    let state = Rc::new(RefCell::new(TipState {
        gen: 0,
        layer: None,
        focused: false,
    }));
    {
        let state = state.clone();
        let text = text.clone();
        cx.on_cleanup(move || {
            let mut s = state.borrow_mut();
            s.hide();
            if s.focused {
                if let Some(f) = focus_line() {
                    if f.with_untracked(|l| l.as_deref() == Some(text.as_str())) {
                        f.set(None);
                    }
                }
            }
        });
    }
    // A press anywhere on the control hides its tip (the control acts —
    // a modal it opens must not wear the tip). Capture: the control's own
    // handler stops the press before it bubbles.
    let st_press = state.clone();
    let el = el.on(Phase::Capture, move |_ctx, ev| {
        let pressed = match ev {
            UiEvent::Mouse(m) => matches!(m.kind, abstracttui::ui::MouseKind::Down(_)),
            UiEvent::Key(k) => matches!(k.key, Key::Enter | Key::Char(' ')),
            _ => false,
        };
        if pressed {
            let mut s = st_press.borrow_mut();
            s.gen += 1;
            s.hide();
        }
    });
    el.on(Phase::Bubble, move |ctx, ev| match ev {
        UiEvent::MouseEnter => {
            let anchor = ctx.current_rect_screen();
            let gen = {
                let mut s = state.borrow_mut();
                s.gen += 1;
                s.gen
            };
            let state = state.clone();
            let text = text.clone();
            after(HOVER_DELAY, move || {
                let mut s = state.borrow_mut();
                if s.gen != gen || s.layer.is_some() {
                    return;
                }
                s.layer = show(anchor, &text);
            });
        }
        UiEvent::MouseLeave => {
            let mut s = state.borrow_mut();
            s.gen += 1;
            if !s.focused {
                s.hide();
            }
        }
        UiEvent::FocusIn => {
            let anchor = ctx.current_rect_screen();
            let mut s = state.borrow_mut();
            s.focused = true;
            s.gen += 1;
            s.hide();
            s.layer = show(anchor, &text);
            if let Some(f) = focus_line() {
                f.set(Some(text.clone()));
            }
        }
        UiEvent::FocusOut => {
            let mut s = state.borrow_mut();
            s.focused = false;
            s.gen += 1;
            s.hide();
            if let Some(f) = focus_line() {
                if f.with_untracked(|l| l.as_deref() == Some(text.as_str())) {
                    f.set(None);
                }
            }
        }
        _ => {}
    })
}

/// Paint the tip label against `anchor` (screen cells).
fn show(anchor: Rect, text: &str) -> Option<LayerHandle> {
    let overlays = OVERLAYS.with(|o| o.borrow().clone())?;
    let vp = current_viewport();
    if vp.w <= 2 || vp.h <= 1 {
        return None;
    }
    let w = (abstracttui::text::width(text) + 2).min(vp.w);
    let below = anchor.y + anchor.h;
    let y = if below < vp.h - 1 {
        below
    } else {
        (anchor.y - 1).max(0)
    };
    let x = anchor.x.min(vp.w - w).max(0);
    let rect = Rect::new(x, y, w, 1);
    let t = &current_theme().tokens;
    let ink = t.text;
    let ground = t.surface_raised;
    let border = t.border_focus;
    let label = super::paint::fit(text, w - 2);
    Some(
        overlays.layer_draw(overlays.top_z() + 1, rect, move |canvas, rect| {
            let st = Style::new().fg(ink).bg(ground);
            canvas.fill_styled(rect, ' ', &st);
            canvas.print_styled(
                Point::new(rect.x, rect.y),
                "▏",
                &Style::new().fg(border).bg(ground),
            );
            canvas.print_styled(Point::new(rect.x + 1, rect.y), &label, &st);
        }),
    )
}
