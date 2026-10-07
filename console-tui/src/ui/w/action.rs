//! Actions and action buttons (DESIGN-TUI.md §4.1). One `Vec<Action>`
//! per row is the single source for the row's buttons, the hint bar,
//! the `?` panel and the click tests.

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

/// Icon button (the web shows an icon) or labelled button.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Display {
    Glyph,
    Label,
}

/// Normal or destructive (hover ink `error`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Normal,
    Danger,
}

/// One action a row (or a page) offers.
#[derive(Clone, Debug)]
pub struct Action {
    /// Stable id: the on-action callback key, the click-test key.
    pub id: &'static str,
    /// The web wording (button label, hint bar, `?` panel).
    pub label: String,
    /// Glyph from `glyphs::GLYPHS` (Display::Glyph).
    pub glyph: &'static str,
    pub display: Display,
    /// Accelerator on the selected row / page.
    pub key: Option<char>,
    /// The web tooltip (falls back to `label`).
    pub tooltip: Option<String>,
    /// `Err(reason)`: faint, the reason in the tooltip, presses do nothing.
    pub enabled: Result<(), String>,
    pub tone: Tone,
}

impl Action {
    /// A glyph action (icon button on the web).
    pub fn glyph(id: &'static str, label: impl Into<String>) -> Action {
        Action {
            id,
            label: label.into(),
            glyph: super::glyphs::glyph(id, true),
            display: Display::Glyph,
            key: None,
            tooltip: None,
            enabled: Ok(()),
            tone: Tone::Normal,
        }
    }
    /// A labelled action (labelled button on the web).
    pub fn label(id: &'static str, label: impl Into<String>) -> Action {
        Action {
            id,
            label: label.into(),
            glyph: "",
            display: Display::Label,
            key: None,
            tooltip: None,
            enabled: Ok(()),
            tone: Tone::Normal,
        }
    }
    pub fn key(mut self, k: char) -> Action {
        self.key = Some(k);
        self
    }
    pub fn tooltip(mut self, t: impl Into<String>) -> Action {
        self.tooltip = Some(t.into());
        self
    }
    pub fn refused(mut self, why: Option<String>) -> Action {
        if let Some(w) = why {
            self.enabled = Err(w);
        }
        self
    }
    pub fn danger(mut self) -> Action {
        self.tone = Tone::Danger;
        self
    }
    pub fn is_enabled(&self) -> bool {
        self.enabled.is_ok()
    }
    /// The tooltip text: the web tooltip (or label), the accelerator,
    /// and the refusal reason when disabled.
    pub fn tip_text(&self) -> String {
        let head = self.tooltip.clone().unwrap_or_else(|| self.label.clone());
        let mut s = head;
        if let Some(k) = self.key {
            s.push_str(&format!("  ({k})"));
        }
        if let Err(why) = &self.enabled {
            s.push_str(&format!(" — {why}"));
        }
        s
    }
    /// What the button shows.
    pub fn face(&self) -> String {
        match self.display {
            Display::Glyph => self.glyph.to_string(),
            Display::Label => self.label.clone(),
        }
    }
    /// Cells the button takes (glyph: 1 + 1 pad; label: label + 2 pads).
    pub fn width(&self) -> i32 {
        match self.display {
            Display::Glyph => 2,
            Display::Label => abstracttui::text::width(&self.label) + 2,
        }
    }
}

/// Ground the button sits on: chips need a ground that differs from it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum On {
    /// A page (surface): chip = surface_raised.
    Page,
    /// A modal or a raised card: chip = bg.
    Raised,
    /// A selected table row (selection pair): chip = surface.
    Selected,
}

/// A clickable button for `a`. `tab_stop`: whether Tab reaches it (a
/// table's unselected rows keep their buttons clickable but out of the
/// Tab order, so Tab from the table lands on the SELECTED row's actions).
pub fn button(
    cx: Scope,
    t: &TokenSet,
    a: &Action,
    on: On,
    tab_stop: bool,
    mut on_press: impl FnMut() + 'static,
) -> View {
    let face = a.face();
    let w = a.width();
    let disabled = !a.is_enabled();
    let glyph = a.display == Display::Glyph;
    let fg = if glyph { t.accent } else { t.text };
    let ground = match on {
        On::Page => t.surface_raised,
        On::Raised => t.bg,
        On::Selected => t.surface,
    };
    let bg = if glyph && on != On::Selected {
        None
    } else {
        Some(ground)
    };
    let hover_ink = if a.tone == Tone::Danger {
        t.error
    } else {
        t.accent
    };
    let (focus_fg, focus_bg, faint) = (t.selection_fg, t.selection_bg, t.text_faint);
    let hovered = cx.signal(false);
    let focused = cx.signal(false);
    let pressed = cx.signal(false);
    let press: Rc<RefCell<Box<dyn FnMut()>>> = Rc::new(RefCell::new(Box::new(move || on_press())));
    let mut el = Element::new()
        .style(
            LayoutStyle::default()
                .width(Dimension::Cells(w))
                .height(Dimension::Cells(1))
                .shrink(0.0),
        )
        .role(abstracttui::ui::Role::Button)
        .access_label(a.label.clone())
        .hover_signal(hovered)
        .focus_signal(focused);
    if !disabled {
        if tab_stop {
            el = el.focusable();
        }
        let p1 = press.clone();
        el = el.on(Phase::Bubble, move |ctx, ev| match ev {
            UiEvent::Key(k)
                if (k.key == Key::Enter || k.key == Key::Char(' ')) && k.mods.0 == 0 =>
            {
                if focused.get_untracked() {
                    ctx.stop_propagation();
                    (p1.borrow_mut())();
                }
            }
            UiEvent::Mouse(m) => match m.kind {
                MouseKind::Down(MouseButton::Left) => {
                    pressed.set(true);
                    ctx.capture_pointer(ctx.current().expect("button view"));
                    ctx.stop_propagation();
                }
                MouseKind::Up(MouseButton::Left) => {
                    let inside = ctx.current_rect().contains(m.pos);
                    let clicks = pressed.get_untracked() && inside;
                    pressed.set(false);
                    ctx.release_pointer();
                    ctx.stop_propagation();
                    if clicks {
                        (p1.borrow_mut())();
                    }
                }
                _ => {}
            },
            _ => {}
        });
    } else {
        el = el.access_value(|| "disabled".into());
    }
    let el = el.child(dyn_view(LayoutStyle::fill(), move || {
        let (h, f, p) = (hovered.get(), focused.get(), pressed.get());
        let (ink, ground, bold) = if disabled {
            (faint, bg, false)
        } else if p || f {
            (focus_fg, Some(focus_bg), p)
        } else if h {
            (hover_ink, bg, true)
        } else {
            (fg, bg, false)
        };
        let face = face.clone();
        Element::new()
            .style(LayoutStyle::fill())
            .draw(move |canvas, rect| {
                if rect.is_empty() {
                    return;
                }
                let mut st = Style::new().fg(ink);
                if let Some(g) = ground {
                    st = st.bg(g);
                    canvas.fill_styled(rect, ' ', &st);
                }
                if bold {
                    st = st.attrs(Attrs::BOLD);
                }
                let fw = abstracttui::text::width(&face);
                let x = if glyph {
                    rect.x
                } else {
                    rect.x + ((rect.w - fw).max(0)) / 2
                };
                canvas.print_styled(Point::new(x, rect.y), &face, &st);
            })
            .build()
    }));
    super::tip::with_tip(cx, el, a.tip_text()).build()
}

/// A row of action buttons (one cell gap between labelled buttons).
pub struct RowActions {
    pub actions: Vec<Action>,
}

impl RowActions {
    pub fn new(actions: Vec<Action>) -> RowActions {
        RowActions { actions }
    }

    /// Cells needed on ONE line.
    pub fn width(&self) -> i32 {
        let n = self.actions.len() as i32;
        let gaps = self
            .actions
            .iter()
            .filter(|a| a.display == Display::Label)
            .count() as i32;
        self.actions.iter().map(Action::width).sum::<i32>() + gaps.min((n - 1).max(0))
    }

    /// Lay the buttons out within `max_w` cells, wrapping onto further
    /// lines between buttons (never inside one). Returns (view, lines).
    pub fn view(
        self,
        cx: Scope,
        t: &TokenSet,
        on: On,
        tab_stop: bool,
        max_w: i32,
        on_action: Rc<dyn Fn(&'static str)>,
    ) -> (View, i32) {
        let mut lines: Vec<Vec<Action>> = vec![Vec::new()];
        let mut used = 0;
        for a in self.actions {
            let w = a.width() + if a.display == Display::Label { 1 } else { 0 };
            if used > 0 && used + a.width() > max_w {
                lines.push(Vec::new());
                used = 0;
            }
            used += w;
            lines.last_mut().expect("line").push(a);
        }
        let n = lines.len() as i32;
        let mut col = Element::new().style(LayoutStyle::column().shrink(0.0));
        for line in lines {
            let mut row = Element::new().style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0));
            for a in line {
                let id = a.id;
                let cb = on_action.clone();
                let label_gap = a.display == Display::Label;
                row = row.child(button(cx, t, &a, on, tab_stop, move || cb(id)));
                if label_gap {
                    row = row.child(
                        Element::new()
                            .style(LayoutStyle::default().width(Dimension::Cells(1)).shrink(0.0))
                            .build(),
                    );
                }
            }
            col = col.child(row.build());
        }
        (col.build(), n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widths_and_tips() {
        let a = Action::glyph("archive", "Archive")
            .tooltip("Archive alice (kept, hidden)")
            .key('d');
        assert_eq!(a.width(), 2);
        assert_eq!(a.tip_text(), "Archive alice (kept, hidden)  (d)");
        let b = Action::label("open", "Open").refused(Some("Only an admin can".into()));
        assert_eq!(b.width(), 6);
        assert!(b.tip_text().ends_with("— Only an admin can"));
        let r = RowActions::new(vec![a, b]);
        assert_eq!(r.width(), 9); // 2 + 6 + one gap after the label
    }
}
