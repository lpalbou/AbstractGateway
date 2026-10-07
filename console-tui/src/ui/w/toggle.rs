//! Toggle (DESIGN-TUI.md §4.4): a state-showing switch — `━●` on (ok
//! ink), `●─` off (muted), faint when it cannot be switched here (the
//! reason in its tooltip). The feature label follows when given (table
//! cells omit it: the column header is the label). No verbs: the label
//! names the feature, the glyph shows the state.
//!
//! Immediate mode: the toggle shows `on` and asks `on_change(!on)`; the
//! caller writes (one PUT) and the store's answer re-renders it.

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::Style;
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

pub const ON: &str = "━●";
pub const OFF: &str = "●─";

pub struct Toggle {
    on: bool,
    label: Option<String>,
    allowed: Result<(), String>,
    tip: Option<String>,
    busy: bool,
    tab_stop: bool,
    on_change: Option<Box<dyn FnMut(bool)>>,
}

impl Toggle {
    pub fn new(on: bool) -> Toggle {
        Toggle {
            on,
            label: None,
            allowed: Ok(()),
            tip: None,
            busy: false,
            tab_stop: true,
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
    pub fn busy(mut self, b: bool) -> Toggle {
        self.busy = b;
        self
    }
    pub fn tab_stop(mut self, s: bool) -> Toggle {
        self.tab_stop = s;
        self
    }
    pub fn on_change(mut self, f: impl FnMut(bool) + 'static) -> Toggle {
        self.on_change = Some(Box::new(f));
        self
    }

    /// Cells it takes on one line.
    pub fn width(&self) -> i32 {
        2 + self
            .label
            .as_ref()
            .map(|l| 1 + abstracttui::text::width(l))
            .unwrap_or(0)
    }

    pub fn view(self, cx: Scope, t: &TokenSet) -> View {
        let on = self.on;
        let allowed = self.allowed.is_ok() && !self.busy;
        let glyph = if self.busy {
            "◌─".to_string()
        } else if on {
            ON.to_string()
        } else {
            OFF.to_string()
        };
        let label = self.label.clone();
        let w = self.width();
        let (ok, muted, faint, text) = (t.ok, t.text_muted, t.text_faint, t.text);
        let (sel_fg, sel_bg, accent) = (t.selection_fg, t.selection_bg, t.accent);
        let hovered = cx.signal(false);
        let focused = cx.signal(false);
        let cb: Rc<RefCell<Option<Box<dyn FnMut(bool)>>>> = Rc::new(RefCell::new(self.on_change));
        let mut el = Element::new()
            .style(
                LayoutStyle::default()
                    .width(Dimension::Cells(w))
                    .height(Dimension::Cells(1))
                    .shrink(0.0),
            )
            .role(abstracttui::ui::Role::Checkbox)
            .access_label(label.clone().unwrap_or_default())
            .access_value(move || if on { "on".into() } else { "off".into() })
            .hover_signal(hovered)
            .focus_signal(focused);
        if allowed {
            if self.tab_stop {
                el = el.focusable();
            }
            let cb1 = cb.clone();
            el = el.on(Phase::Bubble, move |ctx, ev| {
                let fire = |cb: &Rc<RefCell<Option<Box<dyn FnMut(bool)>>>>| {
                    if let Some(f) = cb.borrow_mut().as_mut() {
                        f(!on);
                    }
                };
                match ev {
                    UiEvent::Key(k)
                        if (k.key == Key::Char(' ') || k.key == Key::Enter) && k.mods.0 == 0 =>
                    {
                        if focused.get_untracked() {
                            ctx.stop_propagation();
                            fire(&cb1);
                        }
                    }
                    UiEvent::Mouse(m) if matches!(m.kind, MouseKind::Down(MouseButton::Left)) => {
                        ctx.stop_propagation();
                        fire(&cb1);
                    }
                    _ => {}
                }
            });
        }
        let el = el.child(dyn_view(LayoutStyle::fill(), move || {
            let (h, f) = (hovered.get(), focused.get());
            let glyph = glyph.clone();
            let label = label.clone();
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
                    let mut ls = Style::new().fg(if allowed { text } else { faint });
                    if f {
                        gs = Style::new().fg(sel_fg).bg(sel_bg);
                        ls = Style::new().fg(sel_fg).bg(sel_bg);
                        canvas.fill_styled(rect, ' ', &ls);
                    } else if h && allowed {
                        ls = ls.fg(accent);
                    }
                    canvas.print_styled(Point::new(rect.x, rect.y), &glyph, &gs);
                    if let Some(l) = &label {
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
