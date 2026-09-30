//! The terminal's on/off setting (AbstractUIC `docs/state-toggles.md`,
//! "Terminal clients"): one marker followed by the FEATURE name.
//!
//! | State       | Marker                                                  |
//! |-------------|---------------------------------------------------------|
//! | On          | `[x] Job failed`, highlighted (accent ink, bold)        |
//! | Off         | `[ ] Job failed`, plain                                 |
//! | Unavailable | `[-] Agent email tools — Connect a mailbox first.`, dim |
//!
//! Space, Enter or a click switches it. An unavailable switch still takes
//! the focus (the reason must be reachable from the keyboard) and a press
//! says the reason in the status line instead of doing anything. The label
//! never names an action: no "Turn on", no "Email off".
//!
//! Two modes:
//! - immediate (`on_request`): the press asks for the other state and the
//!   caller writes it; the shown state stays the gateway's until the write
//!   is verified and republished (a refused write never shows as applied);
//! - form field (no `on_request`): the press flips the bound signal, which
//!   the form saves with the rest of its fields.

use abstracttui::base::Point;
use abstracttui::layout::{Dimension, Style as LayoutStyle};
use abstracttui::prelude::*;
use abstracttui::render::{Attrs, Style};
use abstracttui::ui::{dyn_view, Element, MouseButton, MouseKind, Phase, Role, UiEvent};

/// `[x]` / `[ ]` / `[-]`: the state reads without colour.
pub fn marker(on: bool, unavailable: bool) -> &'static str {
    if unavailable {
        "[-]"
    } else if on {
        "[x]"
    } else {
        "[ ]"
    }
}

/// The whole switch line as text (marker, label, the reason after an em
/// dash when unavailable, a busy note while a write is in flight).
pub fn switch_text(label: &str, on: bool, unavailable: Option<&str>, busy: bool) -> String {
    let mut out = format!("{} {label}", marker(on, unavailable.is_some()));
    if let Some(why) = unavailable {
        out.push_str(" — ");
        out.push_str(why);
    }
    if busy {
        out.push_str(" · saving…");
    }
    out
}

/// The ink of an unfocused switch: accent + bold when on, plain when off,
/// faint when unavailable.
pub fn switch_style(t: &TokenSet, on: bool, unavailable: bool) -> Style {
    if unavailable {
        Style::new().fg(t.text_faint).bg(t.surface)
    } else if on {
        Style::new().fg(t.accent).bg(t.surface).attrs(Attrs::BOLD)
    } else {
        Style::new().fg(t.text).bg(t.surface)
    }
}

type Request = Box<dyn FnMut(bool)>;
type Busy = std::rc::Rc<dyn Fn() -> bool>;

/// The key hint every surface with a switch shows (never "turn on/off").
pub const KEY_HINT: (&str, &str) = ("space", "switch");

pub struct Switch {
    label: String,
    checked: Signal<bool>,
    unavailable: Option<String>,
    busy: Busy,
    on_request: Option<Request>,
    notice: Option<Signal<Option<String>>>,
    layout: Option<LayoutStyle>,
}

impl Switch {
    pub fn new(label: impl Into<String>, checked: Signal<bool>) -> Switch {
        Switch {
            label: label.into(),
            checked,
            unavailable: None,
            busy: std::rc::Rc::new(|| false),
            on_request: None,
            notice: None,
            layout: None,
        }
    }

    /// A non-empty reason makes the switch unavailable.
    pub fn unavailable(mut self, reason: Option<String>) -> Switch {
        self.unavailable = reason.filter(|r| !r.trim().is_empty());
        self
    }

    /// A write is in flight: presses are ignored until it lands.
    pub fn busy(mut self, busy: bool) -> Switch {
        self.busy = std::rc::Rc::new(move || busy);
        self
    }

    /// Busy while `f` says so — read reactively, so the switch shows
    /// "saving…" without being rebuilt (a rebuild would drop the focus).
    pub fn busy_when(mut self, f: impl Fn() -> bool + 'static) -> Switch {
        self.busy = std::rc::Rc::new(f);
        self
    }

    /// Immediate apply: called with the REQUESTED state; the bound signal
    /// is left alone (the caller republishes the verified truth).
    pub fn on_request(mut self, f: impl FnMut(bool) + 'static) -> Switch {
        self.on_request = Some(Box::new(f));
        self
    }

    /// Where an unavailable press says its reason.
    pub fn notice(mut self, notice: Signal<Option<String>>) -> Switch {
        self.notice = Some(notice);
        self
    }

    /// Take the row's whole width (a switch alone on its row: room for
    /// the "saving…" note).
    pub fn fill(self) -> Switch {
        self.layout(
            LayoutStyle::default()
                .width(Dimension::Percent(1.0))
                .height(Dimension::Cells(1))
                .shrink(0.0),
        )
    }

    pub fn layout(mut self, layout: LayoutStyle) -> Switch {
        self.layout = Some(layout);
        self
    }

    pub fn element(self, cx: Scope, t: &TokenSet) -> Element {
        let tokens = *t;
        let Switch {
            label,
            checked,
            unavailable,
            busy,
            on_request,
            notice,
            layout,
        } = self;
        // Sized to the label (and reason); the busy note paints into the
        // row when the caller gives the switch the row's width (`fill`).
        let width_text = switch_text(&label, true, unavailable.as_deref(), false);
        let width = abstracttui::text::width(&width_text);
        let layout = layout.unwrap_or_else(|| {
            LayoutStyle::default()
                .width(Dimension::Cells(width))
                .height(Dimension::Cells(1))
                .shrink(0.0)
        });
        let focused = cx.signal(false);
        let request = std::rc::Rc::new(std::cell::RefCell::new(on_request));
        let reason = unavailable.clone();
        let press = {
            let request = request.clone();
            let busy = busy.clone();
            move || {
                if let Some(why) = &reason {
                    if let Some(n) = notice {
                        n.set(Some(why.clone()));
                    }
                    return;
                }
                if busy() {
                    return;
                }
                let want = !checked.get_untracked();
                let mut slot = request.borrow_mut();
                match slot.as_mut() {
                    Some(f) => f(want),
                    None => checked.set(want),
                }
            }
        };
        let access_label = label.clone();
        let access_reason = unavailable.clone();
        Element::new()
            .style(layout)
            .role(Role::Checkbox)
            .access_label(access_label)
            .access_value(move || match &access_reason {
                Some(why) => format!("unavailable: {why}"),
                None => if checked.get_untracked() { "on" } else { "off" }.into(),
            })
            .focus_signal(focused)
            .focusable()
            .on(Phase::Bubble, move |ctx, ev| match ev {
                UiEvent::Key(k) if k.key == Key::Enter || k.key == Key::Char(' ') => {
                    if focused.get_untracked() {
                        press();
                        ctx.stop_propagation();
                    }
                }
                UiEvent::Mouse(m) if matches!(m.kind, MouseKind::Down(MouseButton::Left)) => {
                    press();
                    ctx.stop_propagation();
                }
                _ => {}
            })
            .child(dyn_view(
                LayoutStyle::default()
                    .width(Dimension::Percent(1.0))
                    .height(Dimension::Cells(1)),
                move || {
                    let on = checked.get();
                    let focus = focused.get();
                    let text = switch_text(&label, on, unavailable.as_deref(), busy());
                    let unavail = unavailable.is_some();
                    Element::new()
                        .style(
                            LayoutStyle::default()
                                .width(Dimension::Percent(1.0))
                                .height(Dimension::Cells(1)),
                        )
                        .draw(move |canvas, rect| {
                            let base = switch_style(&tokens, on, unavail);
                            let style = if focus {
                                // The focus pair keeps the on-state weight so
                                // a focused ON switch still reads as on.
                                let s =
                                    Style::new().fg(tokens.selection_fg).bg(tokens.selection_bg);
                                if on && !unavail {
                                    s.attrs(Attrs::BOLD)
                                } else {
                                    s
                                }
                            } else {
                                base
                            };
                            canvas.fill_styled(rect, ' ', &style);
                            canvas.print_styled(Point::new(rect.x, rect.y), &text, &style);
                        })
                        .build()
                },
            ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_marker_and_text_name_the_feature_and_the_state() {
        assert_eq!(
            switch_text("Job failed", true, None, false),
            "[x] Job failed"
        );
        assert_eq!(
            switch_text("Job failed", false, None, false),
            "[ ] Job failed"
        );
        assert_eq!(
            switch_text(
                "Agent email tools",
                false,
                Some("Connect a mailbox first."),
                false
            ),
            "[-] Agent email tools — Connect a mailbox first."
        );
        assert_eq!(
            switch_text("Active", true, None, true),
            "[x] Active · saving…"
        );
    }

    #[test]
    fn on_is_highlighted_off_is_plain_unavailable_is_faint() {
        let t = abstracttui::theme::default_theme().tokens;
        let on = switch_style(&t, true, false);
        let off = switch_style(&t, false, false);
        let na = switch_style(&t, true, true);
        assert_eq!(
            on,
            Style::new().fg(t.accent).bg(t.surface).attrs(Attrs::BOLD)
        );
        assert_eq!(off, Style::new().fg(t.text).bg(t.surface));
        assert_eq!(na, Style::new().fg(t.text_faint).bg(t.surface));
    }
}
