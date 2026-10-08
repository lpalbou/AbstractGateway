//! THE confirmation dialog (DESIGN-TUI.md §2.6, R15 ruling F1): the web's
//! sentence and two buttons named after what they do — `[Rotate] [Cancel]`.
//! A click on the action button does it; Enter does it only while that
//! button has the focus; Esc, Cancel and a click outside keep things as
//! they are. A destructive confirm opens with the focus on Cancel; a
//! plain one (Install…) on the action. No screen builds its own prompt.
//!
//! The dialog is its own modal layer ABOVE every open layer (a confirm
//! asked from inside a form stacks over the form and gets the keys), and
//! the page stays visible around it.

use std::cell::RefCell;
use std::rc::Rc;

use abstracttui::app::{Overlays, MODAL_Z};
use abstracttui::base::Rect;
use abstracttui::prelude::*;

use super::super::UiState;
use super::action::{button, button_focused, Action, On};
use super::paint::{fill_line, wrap, Ink};

thread_local! {
    /// The confirmations shown (tests read the sentence; bounded).
    static LOG: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
}

/// The sentences of the confirmations opened so far (newest last).
pub fn asked() -> Vec<String> {
    LOG.with(|l| l.borrow().clone())
}

pub struct Confirm {
    sentence: String,
    go: String,
    keep: String,
    danger: bool,
}

impl Confirm {
    /// A destructive confirm: the focus starts on `keep`.
    pub fn danger(sentence: impl Into<String>, go: &str, keep: &str) -> Confirm {
        Confirm {
            sentence: sentence.into(),
            go: go.into(),
            keep: keep.into(),
            danger: true,
        }
    }
    /// A plain confirm (an install, a download): the focus starts on `go`.
    pub fn plain(sentence: impl Into<String>, go: &str, keep: &str) -> Confirm {
        Confirm {
            danger: false,
            ..Confirm::danger(sentence, go, keep)
        }
    }

    /// Open; `on_yes` runs once when the action button is pressed.
    pub fn open(self, cx: Scope, ui: UiState, on_yes: impl FnOnce() + 'static) {
        self.open_with(cx, ui, on_yes, || {});
    }

    /// Open; `on_yes` on the action, `on_no` on Cancel / Esc.
    pub fn open_with(
        self,
        cx: Scope,
        ui: UiState,
        on_yes: impl FnOnce() + 'static,
        on_no: impl FnOnce() + 'static,
    ) {
        let Some(ov) = cx.use_context::<Overlays>() else {
            return;
        };
        LOG.with(|l| {
            let mut l = l.borrow_mut();
            if l.len() > 64 {
                l.remove(0);
            }
            l.push(self.sentence.clone());
        });
        let vp = abstracttui::app::use_viewport(cx).get_untracked();
        let go_a = {
            let a = Action::label("confirm", self.go.clone());
            if self.danger {
                a.danger()
            } else {
                a
            }
        };
        let keep_a = Action::label("keep", self.keep.clone());
        let buttons_w = go_a.width() + 1 + keep_a.width();
        let text_w = (vp.w - 8).clamp(20, 90).max(buttons_w);
        let lines = wrap(&self.sentence, text_w);
        let longest = lines
            .iter()
            .map(|l| abstracttui::text::width(l))
            .max()
            .unwrap_or(0)
            .max(buttons_w);
        let size = Size::new((longest + 6).min(vp.w), (lines.len() as i32 + 5).min(vp.h));
        let bounds = Rect::new(
            ((vp.w - size.w) / 2).max(0),
            ((vp.h - size.h) / 2).max(0),
            size.w,
            size.h,
        );
        let scope = cx.child();
        // Settled once: the first of action / cancel wins.
        type Once = Rc<RefCell<Option<Box<dyn FnOnce()>>>>;
        let yes: Once = Rc::new(RefCell::new(Some(Box::new(on_yes))));
        let no: Once = Rc::new(RefCell::new(Some(Box::new(on_no))));
        let layer_slot: Rc<RefCell<Option<abstracttui::app::LayerHandle>>> =
            Rc::new(RefCell::new(None));
        ui.prompt_open.update(|n| *n += 1);
        let finish = {
            let (yes, no, slot) = (yes.clone(), no.clone(), layer_slot.clone());
            Rc::new(move |go: bool| {
                let (y, n) = (yes.borrow_mut().take(), no.borrow_mut().take());
                if y.is_none() && n.is_none() {
                    return;
                }
                if let Some(l) = slot.borrow_mut().take() {
                    l.remove();
                }
                ui.prompt_open.update(|n| *n = n.saturating_sub(1));
                if go {
                    if let Some(f) = y {
                        f();
                    }
                } else if let Some(f) = n {
                    f();
                }
                scope.dispose();
            })
        };
        let t = abstracttui::app::current_theme().tokens;
        let mut col = Element::new().style(LayoutStyle::column().grow(1.0));
        for l in &lines {
            col = col.child(fill_line(
                LayoutStyle::line(1).shrink(0.0),
                vec![Ink::new(l.clone(), t.text)],
                None,
            ));
        }
        col = col.child(
            Element::new()
                .style(LayoutStyle::default().grow(1.0))
                .build(),
        );
        let (f_go, f_keep, f_esc) = (finish.clone(), finish.clone(), finish.clone());
        let go_btn = if self.danger {
            button(scope, &t, &go_a, On::Raised, true, move || f_go(true))
        } else {
            button_focused(scope, &t, &go_a, On::Raised, move || f_go(true))
        };
        let keep_btn = if self.danger {
            button_focused(scope, &t, &keep_a, On::Raised, move || f_keep(false))
        } else {
            button(scope, &t, &keep_a, On::Raised, true, move || f_keep(false))
        };
        col = col.child(super::form::button_row(vec![go_btn, keep_btn]));
        let panel = Element::new()
            .style(LayoutStyle::fill())
            .role(abstracttui::ui::Role::Dialog)
            .focus_trap()
            .shortcut(KeyChord::plain(Key::Escape), move |_| f_esc(false))
            .child(
                Block::new()
                    .border(BorderKind::Rounded)
                    .fill(t.surface_raised)
                    .layout(LayoutStyle::column().grow(1.0).padding(Edges::hv(1, 0)))
                    .child(col.build())
                    .element(&t)
                    .build(),
            )
            .build();
        let z = (ov.top_z() + 1).max(MODAL_Z + 1);
        let layer = ov.layer_tree(z, bounds, true, scope, panel);
        *layer_slot.borrow_mut() = Some(layer);
    }
}
