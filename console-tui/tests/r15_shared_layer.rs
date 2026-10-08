//! R15 shared layer (lead-owned `ui/w`): behaviours the screen workers
//! rely on, pinned outside any one screen.
//! - A confirmation taller than the terminal scrolls its sentence and keeps
//!   its `[Action] [Cancel]` row on screen (R15-B: the gateway's Internet
//!   acknowledgement lists every warning).

mod r8w4;

use std::cell::Cell;

use abstractgateway_console::ui::{self, w};
use abstracttui::prelude::*;
use r8w4::{harness, Mount};

thread_local! {
    static YES: Cell<u32> = const { Cell::new(0) };
}

fn long_sentence() -> String {
    (1..=30)
        .map(|i| format!("Warning {i}: this line is one of the many the gateway lists."))
        .collect::<Vec<_>>()
        .join(" ")
}

fn confirm_page(ctx: &ui::Ctx, cx: Scope) -> View {
    let t = use_theme(cx).get().tokens;
    let ui = ctx.ui;
    let a = w::Action::label("ask", "Ask");
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .child(w::action::button(
            cx,
            &t,
            &a,
            w::action::On::Page,
            true,
            move || {
                w::Confirm::danger(long_sentence(), "Acknowledge", "Cancel")
                    .open(cx, ui, || YES.with(|y| y.set(y.get() + 1)));
            },
        ))
        .build()
}

#[test]
fn a_confirmation_taller_than_the_screen_scrolls_and_keeps_its_buttons() {
    YES.with(|y| y.set(0));
    let mut h = harness((80, 24), Mount::Page(confirm_page));
    h.turns(2);
    let s = h.click_text("Ask");
    let s = if s.contains("Acknowledge") {
        s
    } else {
        h.turns(2)
    };
    assert!(
        s.contains(" Acknowledge ") && s.contains(" Cancel "),
        "the button row is on screen at 80x24:\n{s}"
    );
    assert!(
        s.contains("Warning 1:"),
        "the sentence starts at the top:\n{s}"
    );
    assert!(
        !s.contains("Warning 30:"),
        "the tail is below the fold:\n{s}"
    );
    // PgDn twice reaches the end; the buttons stay.
    h.key(b"\x1b[6~");
    h.key(b"\x1b[6~");
    h.key(b"\x1b[6~");
    let s = h.turns(2);
    assert!(s.contains("Warning 30:"), "PgDn scrolls to the tail:\n{s}");
    assert!(s.contains(" Acknowledge ") && s.contains(" Cancel "), "{s}");
    // The action still answers a click.
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(" Acknowledge ") && l.contains(" Cancel "))
        .last()
        .unwrap();
    let x = line[..line.find(" Acknowledge ").unwrap() + 1]
        .chars()
        .count()
        + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert_eq!(YES.with(|y| y.get()), 1, "Acknowledge fired");
}
