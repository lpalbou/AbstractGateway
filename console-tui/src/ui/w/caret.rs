//! The caret registry (DESIGN-TUI.md §2.2): the shell's Capture-phase
//! ←/→ handler switches screens EXCEPT while a page-level text field has
//! the caret. Every page-level `TextInput`/`TextArea` is wrapped with
//! [`caret_tracked`]; fields inside modals need nothing (a modal is its
//! own overlay tree — the shell root never sees its keys).

use std::cell::Cell;
use std::rc::Rc;

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};

/// `Some(id)` = the text field `id` holds the caret.
pub type Caret = Signal<Option<u64>>;

thread_local! {
    static NEXT: Cell<u64> = const { Cell::new(1) };
}

/// Register `el` (a text field's element) in the caret registry: FocusIn
/// claims the caret, FocusOut releases it, and the field's scope dying
/// while focused (a page switch) releases it too — a stale claim would
/// leave ←/→ dead.
pub fn caret_tracked(cx: Scope, caret: Caret, el: Element) -> Element {
    let id = NEXT.with(|n| {
        let v = n.get();
        n.set(v + 1);
        v
    });
    let alive = Rc::new(Cell::new(true));
    {
        let alive = alive.clone();
        cx.on_cleanup(move || {
            alive.set(false);
            if caret.is_alive() && caret.get_untracked() == Some(id) {
                caret.set(None);
            }
        });
    }
    el.on(Phase::Bubble, move |_ctx, ev| match ev {
        UiEvent::FocusIn => {
            if alive.get() && caret.is_alive() {
                caret.set(Some(id));
            }
        }
        UiEvent::FocusOut if caret.is_alive() && caret.get_untracked() == Some(id) => {
            caret.set(None);
        }
        _ => {}
    })
}
