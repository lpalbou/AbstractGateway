//! Results (DESIGN-TUI.md §2.6, §4.10): success → toast; destructive or
//! irreversible → a must-choose confirmation with the web's sentence.
//! Refusals are NEVER toasts (they vanish): they go inline.

use std::time::Duration;

use abstracttui::app::Toast;
use abstracttui::prelude::*;

use super::super::Ctx;

/// How long a success toast stays.
pub const TOAST_FOR: Duration = Duration::from_secs(3);

thread_local! {
    /// Every toast shown (tests read it: a toast's layer is not in the
    /// root tree text). Bounded.
    static LOG: std::cell::RefCell<Vec<String>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Show a success toast (top-right) and remember it for tests.
pub fn toast(ctx: &Ctx, cx: Scope, msg: impl Into<String>) {
    let msg = msg.into();
    LOG.with(|l| {
        let mut l = l.borrow_mut();
        if l.len() > 64 {
            l.remove(0);
        }
        l.push(msg.clone());
    });
    let vp = abstracttui::app::use_viewport(cx).get_untracked();
    if vp.w > 4 {
        Toast::show_with_motion(&ctx.overlays, cx, vp, msg, TOAST_FOR, Duration::ZERO);
    }
}

/// The toasts shown so far (newest last).
pub fn toasts() -> Vec<String> {
    LOG.with(|l| l.borrow().clone())
}

/// A destructive confirmation: the web sentence, one danger option, one
/// keep option (default), then `on_yes`.
pub fn confirm(
    ctx: &Ctx,
    cx: Scope,
    sentence: impl Into<String>,
    danger: &str,
    keep: &str,
    on_yes: impl FnOnce() + 'static,
) {
    super::super::confirm_danger(cx, ctx.ui, sentence.into(), danger, keep, on_yes);
}
