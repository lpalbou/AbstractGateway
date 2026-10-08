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

thread_local! {
    static OTHER: Cell<u32> = const { Cell::new(0) };
}

fn segment_opens_dialog(ctx: &ui::Ctx, cx: Scope) -> View {
    let t = use_theme(cx).get().tokens;
    let ui = ctx.ui;
    let other = w::Action::label("other", "Other button");
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .child(
            w::Segmented::new(vec!["Local", "Internet"], Some(0))
                .on_pick(move |i| {
                    if i == 1 {
                        w::Confirm::danger("Use Internet mode?", "Use it", "Keep Local").open(
                            cx,
                            ui,
                            || {},
                        );
                    }
                })
                .view(cx, &t),
        )
        .child(w::action::button(
            cx,
            &t,
            &other,
            w::action::On::Page,
            true,
            || OTHER.with(|o| o.set(o.get() + 1)),
        ))
        .build()
}

/// A segment whose pick opens a dialog never keeps the pointer (R15-B
/// trace): after the dialog, a press lands where it lands.
#[test]
fn a_segment_pick_that_opens_a_dialog_never_keeps_the_pointer() {
    OTHER.with(|o| o.set(0));
    let mut h = harness((80, 24), Mount::Page(segment_opens_dialog));
    h.turns(2);
    let s = h.click_text("Internet");
    let s = if s.contains("Use Internet mode?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Use Internet mode?"), "{s}");
    h.esc();
    let s = h.turns(2);
    assert!(!s.contains("Use Internet mode?"), "{s}");
    h.click_text("Other button");
    assert_eq!(
        OTHER.with(|o| o.get()),
        1,
        "the press reached the other button"
    );
    assert!(
        !h.turns(2).contains("Use Internet mode?"),
        "the segment did not take it"
    );
}
