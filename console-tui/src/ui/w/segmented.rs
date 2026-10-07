//! Segmented choice (DESIGN-TUI.md §4.5, A1): a row of real buttons, the
//! chosen one in the selection pair (accent ground), ONE TAB STOP PER
//! SEGMENT — Tab/Shift+Tab reach a segment, Enter/Space or a click picks
//! it. ←/→ are never consumed (the shell owns them). Disabled segments
//! are faint with their reason in the tooltip.

use std::rc::Rc;

use abstracttui::base::Point;
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{MouseButton, MouseKind, Phase, UiEvent};

pub struct Segmented {
    items: Vec<String>,
    chosen: Option<usize>,
    disabled: Vec<(usize, String)>,
    tips: Vec<(usize, String)>,
    vertical: bool,
    on_pick: Option<Rc<dyn Fn(usize)>>,
}

impl Segmented {
    pub fn new<S: Into<String>>(items: impl IntoIterator<Item = S>, chosen: Option<usize>) -> Segmented {
        Segmented {
            items: items.into_iter().map(Into::into).collect(),
            chosen,
            disabled: Vec::new(),
            tips: Vec::new(),
            vertical: false,
            on_pick: None,
        }
    }
    pub fn disable(mut self, i: usize, why: impl Into<String>) -> Segmented {
        self.disabled.push((i, why.into()));
        self
    }
    pub fn tip(mut self, i: usize, t: impl Into<String>) -> Segmented {
        self.tips.push((i, t.into()));
        self
    }
    pub fn vertical(mut self, v: bool) -> Segmented {
        self.vertical = v;
        self
    }
    pub fn on_pick(mut self, f: impl Fn(usize) + 'static) -> Segmented {
        self.on_pick = Some(Rc::new(f));
        self
    }

    /// Cells on one line (each segment = label + 2 pads, 1 gap).
    pub fn width(&self) -> i32 {
        let n = self.items.len() as i32;
        self.items
            .iter()
            .map(|s| abstracttui::text::width(s) + 2)
            .sum::<i32>()
            + (n - 1).max(0)
    }

    pub fn view(self, cx: Scope, t: &TokenSet) -> View {
        let style = if self.vertical {
            LayoutStyle::column().shrink(0.0)
        } else {
            LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0).gap(1)
        };
        let mut row = Element::new().style(style);
        for (i, label) in self.items.iter().enumerate() {
            let why = self.disabled.iter().find(|(k, _)| *k == i).map(|(_, w)| w.clone());
            let tip = self.tips.iter().find(|(k, _)| *k == i).map(|(_, w)| w.clone());
            let chosen = self.chosen == Some(i);
            let pick = self.on_pick.clone();
            row = row.child(segment(cx, t, label.clone(), chosen, why, tip, move || {
                if let Some(p) = &pick {
                    p(i);
                }
            }));
        }
        row.build()
    }
}

fn segment(
    cx: Scope,
    t: &TokenSet,
    label: String,
    chosen: bool,
    disabled: Option<String>,
    tip: Option<String>,
    on_pick: impl Fn() + 'static,
) -> View {
    let w = abstracttui::text::width(&label) + 2;
    let hovered = cx.signal(false);
    let focused = cx.signal(false);
    let off = disabled.is_some();
    let (accent, text, faint, raised) = (t.accent, t.text, t.text_faint, t.surface_raised);
    let (sel_fg, sel_bg, bg) = (t.selection_fg, t.selection_bg, t.bg);
    let mut el = Element::new()
        .style(
            LayoutStyle::default()
                .width(Dimension::Cells(w))
                .height(Dimension::Cells(1))
                .shrink(0.0),
        )
        .role(abstracttui::ui::Role::Button)
        .access_label(label.clone())
        .access_value(move || if chosen { "chosen".into() } else { String::new() })
        .hover_signal(hovered)
        .focus_signal(focused);
    if !off {
        el = el.focusable().on(Phase::Bubble, move |ctx, ev| match ev {
            UiEvent::Key(k) if (k.key == Key::Enter || k.key == Key::Char(' ')) && k.mods.0 == 0 => {
                if focused.get_untracked() {
                    ctx.stop_propagation();
                    on_pick();
                }
            }
            UiEvent::Mouse(m) if matches!(m.kind, MouseKind::Down(MouseButton::Left)) => {
                ctx.stop_propagation();
                on_pick();
            }
            _ => {}
        });
    }
    let el = el.child(dyn_view(LayoutStyle::fill(), move || {
        let (h, f) = (hovered.get(), focused.get());
        let label = label.clone();
        Element::new()
            .style(LayoutStyle::fill())
            .draw(move |canvas, rect| {
                if rect.is_empty() {
                    return;
                }
                let st = if f {
                    Style::new().fg(sel_fg).bg(sel_bg)
                } else if chosen {
                    Style::new().fg(bg).bg(accent).attrs(Attrs::BOLD)
                } else if off {
                    Style::new().fg(faint).bg(raised)
                } else if h {
                    Style::new().fg(accent).bg(raised)
                } else {
                    Style::new().fg(text).bg(raised)
                };
                canvas.fill_styled(rect, ' ', &st);
                canvas.print_styled(Point::new(rect.x + 1, rect.y), &label, &st);
            })
            .build()
    }));
    let tip = match (disabled, tip) {
        (Some(w), _) => w,
        (None, Some(t)) => t,
        (None, None) => String::new(),
    };
    super::tip::with_tip(cx, el, tip).build()
}
