//! Toggle (DESIGN-TUI.md §4.4): a state-showing switch — `━●` on (ok
//! ink), `●─` off (muted) — the current state also while its write is in flight (" · saving…" follows the label) —, faint when
//! it cannot be switched here (the reason in its tooltip). The feature
//! label follows when given (table cells omit it: the column header is
//! the label). No verbs: the label names the feature, the glyph shows the
//! state.
//!
//! Immediate mode: the toggle shows its value and asks `on_change(!on)`;
//! the caller writes (one PUT) and the store's answer re-renders it.
//! `Toggle::bound(sig)` reads the value from a signal INSIDE its paint, so
//! the answer never rebuilds the control (keyboard focus stays on it).

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::Style;
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

pub const ON: &str = "━●";
pub const OFF: &str = "●─";

type Getter = Rc<dyn Fn() -> bool>;

pub struct Toggle {
    value: Getter,
    label: Option<String>,
    allowed: Result<(), String>,
    tip: Option<String>,
    busy: Getter,
    /// Reserve room for " · saving…" (a toggle whose write can be in flight).
    busy_reserve: bool,
    tab_stop: bool,
    autofocus: bool,
    on_change: Option<Box<dyn FnMut(bool)>>,
}

impl Toggle {
    /// A toggle showing a fixed value (rebuilt by its region on change).
    pub fn new(on: bool) -> Toggle {
        Toggle::from_getter(Rc::new(move || on))
    }
    /// A toggle showing `sig` (tracked inside its paint).
    pub fn bound(sig: Signal<bool>) -> Toggle {
        Toggle::from_getter(Rc::new(move || sig.get()))
    }
    /// `Switch::new(label, sig)`'s shape on the Toggle (a bound, labelled
    /// toggle) — the conversion of the old `[x]` switch.
    pub fn switch(label: impl Into<String>, sig: Signal<bool>) -> Toggle {
        Toggle::bound(sig).label(label)
    }
    fn from_getter(value: Getter) -> Toggle {
        Toggle {
            value,
            label: None,
            allowed: Ok(()),
            tip: None,
            busy: Rc::new(|| false),
            busy_reserve: false,
            tab_stop: true,
            autofocus: false,
            on_change: None,
        }
    }
    pub fn label(mut self, l: impl Into<String>) -> Toggle {
        self.label = Some(l.into());
        self
    }
    pub fn allowed(mut self, a: Result<(), String>) -> Toggle {
        self.allowed = a;
        self
    }
    pub fn refused(mut self, why: Option<String>) -> Toggle {
        if let Some(w) = why {
            self.allowed = Err(w);
        }
        self
    }
    pub fn tip(mut self, t: impl Into<String>) -> Toggle {
        self.tip = Some(t.into());
        self
    }
    /// A write is in flight (fixed).
    pub fn busy(mut self, b: bool) -> Toggle {
        self.busy_reserve |= b;
        self.busy = Rc::new(move || b);
        self
    }
    /// A write is in flight (read reactively in the paint).
    pub fn busy_when(mut self, f: impl Fn() -> bool + 'static) -> Toggle {
        self.busy_reserve = true;
        self.busy = Rc::new(f);
        self
    }
    pub fn tab_stop(mut self, s: bool) -> Toggle {
        self.tab_stop = s;
        self
    }
    /// Take the keyboard when mounted (focus restore after a rebuild).
    pub fn autofocus(mut self, a: bool) -> Toggle {
        self.autofocus = a;
        self
    }
    pub fn on_change(mut self, f: impl FnMut(bool) + 'static) -> Toggle {
        self.on_change = Some(Box::new(f));
        self
    }

    /// The text after the glyph: the feature label, then — for a labelled
    /// toggle that cannot be switched — the reason (visible, never
    /// tooltip-only), then " · saving…" while a write is in flight.
    fn tail(label: &Option<String>, refused: &Option<String>, busy: bool) -> Option<String> {
        let l = label.as_ref()?;
        let mut out = l.clone();
        if let Some(why) = refused {
            out.push_str(" — ");
            out.push_str(why);
        }
        if busy {
            out.push_str(" · saving…");
        }
        Some(out)
    }

    /// Cells it takes on one line.
    pub fn width(&self) -> i32 {
        let refused = self.allowed.clone().err();
        2 + Self::tail(&self.label, &refused, self.busy_reserve)
            .map(|l| 1 + abstracttui::text::width(&l))
            .unwrap_or(0)
    }

    pub fn view(self, cx: Scope, t: &TokenSet) -> View {
        let allowed = self.allowed.is_ok();
        let label = self.label.clone();
        let w = self.width();
        let (ok, muted, faint, text) = (t.ok, t.text_muted, t.text_faint, t.text);
        let (sel_fg, sel_bg, accent) = (t.selection_fg, t.selection_bg, t.accent);
        let hovered = cx.signal(false);
        let focused = cx.signal(false);
        let cb: Rc<RefCell<Option<Box<dyn FnMut(bool)>>>> = Rc::new(RefCell::new(self.on_change));
        let value = self.value.clone();
        let busy = self.busy.clone();
        let mut el = Element::new()
            .style(
                LayoutStyle::default()
                    .width(Dimension::Cells(w))
                    .height(Dimension::Cells(1))
                    .shrink(0.0),
            )
            .role(abstracttui::ui::Role::Checkbox)
            .access_label(label.clone().unwrap_or_default())
            .access_value({
                let v = value.clone();
                move || if v() { "on".into() } else { "off".into() }
            })
            .hover_signal(hovered)
            .focus_signal(focused);
        // A refused toggle still takes the focus (its reason must be
        // reachable from the keyboard: the tooltip + status bar show it) and
        // a press says the reason instead of doing anything.
        if !allowed && self.tab_stop {
            el = el.focusable();
            let why = self.allowed.clone().err().unwrap_or_default();
            el = el.on(Phase::Bubble, move |ctx, ev| {
                let pressed = match ev {
                    UiEvent::Key(k) => {
                        (k.key == Key::Char(' ') || k.key == Key::Enter)
                            && k.mods.0 == 0
                            && focused.get_untracked()
                    }
                    UiEvent::Mouse(m) => matches!(m.kind, MouseKind::Down(MouseButton::Left)),
                    _ => false,
                };
                if pressed {
                    ctx.stop_propagation();
                    super::tip::say(&why);
                }
            });
        }
        if allowed {
            if self.tab_stop {
                el = el.focusable();
                if self.autofocus {
                    el = el.autofocus();
                }
            }
            let cb1 = cb.clone();
            let (v1, b1) = (value.clone(), busy.clone());
            el = el.on(Phase::Bubble, move |ctx, ev| {
                let fire = || {
                    if untrack(|| b1()) {
                        return;
                    }
                    let now = untrack(|| v1());
                    if let Some(f) = cb1.borrow_mut().as_mut() {
                        f(!now);
                    }
                };
                match ev {
                    UiEvent::Key(k)
                        if (k.key == Key::Char(' ') || k.key == Key::Enter) && k.mods.0 == 0 =>
                    {
                        if focused.get_untracked() {
                            ctx.stop_propagation();
                            fire();
                        }
                    }
                    UiEvent::Mouse(m) if matches!(m.kind, MouseKind::Down(MouseButton::Left)) => {
                        ctx.stop_propagation();
                        fire();
                    }
                    _ => {}
                }
            });
        }
        let refused = self.allowed.clone().err();
        let el = el.child(dyn_view(LayoutStyle::fill(), move || {
            let (h, f) = (hovered.get(), focused.get());
            let on = value();
            let is_busy = busy();
            let tail = Self::tail(&label, &refused, is_busy);
            // Busy keeps showing the CURRENT state (the gateway's until the
            // write is verified); " · saving…" in the tail says it is moving.
            let glyph = if on { ON } else { OFF };
            Element::new()
                .style(LayoutStyle::fill())
                .draw(move |canvas, rect| {
                    if rect.is_empty() {
                        return;
                    }
                    let gink = if !allowed {
                        faint
                    } else if on {
                        ok
                    } else {
                        muted
                    };
                    let mut gs = Style::new().fg(gink);
                    // The kit's terminal rule (state-toggles.md): ON reads
                    // highlighted (accent, bold), OFF plain, unavailable faint.
                    let mut ls = if !allowed {
                        Style::new().fg(faint)
                    } else if on {
                        Style::new().fg(accent).bold()
                    } else {
                        Style::new().fg(text)
                    };
                    if f {
                        gs = Style::new().fg(sel_fg).bg(sel_bg);
                        ls = Style::new().fg(sel_fg).bg(sel_bg);
                        canvas.fill_styled(rect, ' ', &ls);
                    } else if h && allowed {
                        ls = ls.fg(accent);
                    }
                    canvas.print_styled(Point::new(rect.x, rect.y), glyph, &gs);
                    if let Some(l) = &tail {
                        let l = super::paint::fit(l, rect.w - 3);
                        canvas.print_styled(Point::new(rect.x + 3, rect.y), &l, &ls);
                    }
                })
                .build()
        }));
        let tip = match (&self.allowed, &self.tip) {
            (Err(why), Some(t)) => format!("{t} — {why}"),
            (Err(why), None) => why.clone(),
            (Ok(()), Some(t)) => t.clone(),
            (Ok(()), None) => String::new(),
        };
        super::tip::with_tip(cx, el, tip).build()
    }
}
