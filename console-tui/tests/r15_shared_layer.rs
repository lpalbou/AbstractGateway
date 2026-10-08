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

fn short_confirm_page(ctx: &ui::Ctx, cx: Scope) -> View {
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
                w::Confirm::danger(
                    "Start AbstractGateway at login?",
                    "Start at login",
                    "Leave it",
                )
                .open(cx, ui, || {});
            },
        ))
        .build()
}

/// A2 for confirmations (adversary note a): the status bar's focus line
/// names the confirmation's focused button, and Tab moves it.
#[test]
fn a_confirmations_focused_button_is_named_in_the_status_bar() {
    let mut h = harness((120, 40), Mount::Page(short_confirm_page));
    h.turns(2);
    h.click_text("Ask");
    h.turns(2);
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Leave it"),
        "the focused Cancel is named"
    );
    h.key(b"\t");
    h.turns(2);
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Start at login"),
        "Tab: the action is named"
    );
}

thread_local! {
    static WHEEL_OUT: Cell<u32> = const { Cell::new(0) };
}

fn table_in_page(_ctx: &ui::Ctx, cx: Scope) -> View {
    use abstracttui::ui::{MouseKind, Phase, UiEvent};
    let t = use_theme(cx).get().tokens;
    let sel = cx.signal(None::<String>);
    let rows = (0..30)
        .map(|i| {
            w::Row::new(
                format!("r{i}"),
                vec![w::Cell::Text(vec![w::Ink::new(format!("row {i}"), t.text)])],
            )
        })
        .collect();
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .on(Phase::Bubble, |_e, ev| {
            if let UiEvent::Mouse(m) = ev {
                if matches!(m.kind, MouseKind::ScrollUp | MouseKind::ScrollDown) {
                    WHEEL_OUT.with(|w| w.set(w.get() + 1));
                }
            }
        })
        .child(
            w::DataTable::new(
                vec![w::Col::new("Name", w::ColW::Flex { weight: 1, min: 8 })],
                rows,
                sel,
            )
            .max_rows(5)
            .view(cx, &t),
        )
        .build()
}

/// R15-A: the table owns the wheel only while its window moves — at its
/// top edge a wheel-up bubbles to the page; mid-list it is the table's.
#[test]
fn a_tables_wheel_bubbles_at_its_edge_and_stays_while_it_moves() {
    WHEEL_OUT.with(|w| w.set(0));
    let mut h = harness((80, 24), Mount::Page(table_in_page));
    let s = h.turns(2);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("row 1"))
        .expect(&s);
    let x = line.find("row 1").unwrap() + 2;
    let wheel = |h: &mut r8w4::Harness, b: u8| {
        h.key(format!("\x1b[<{b};{x};{}M", y + 1).as_bytes());
    };
    wheel(&mut h, 64); // up at the top: bubbles
    assert_eq!(
        WHEEL_OUT.with(|w| w.get()),
        1,
        "a wheel-up at the top reaches the page"
    );
    wheel(&mut h, 65); // down: the window moves, the table keeps it
    assert_eq!(
        WHEEL_OUT.with(|w| w.get()),
        1,
        "a moving wheel stays in the table"
    );
}

fn refused_toggle_page(_ctx: &ui::Ctx, cx: Scope) -> View {
    let t = use_theme(cx).get().tokens;
    Element::new()
        .style(LayoutStyle::column().grow(1.0))
        .child(
            w::Toggle::new(false)
                .label("Feature")
                .refused(Some("Only an admin can change this.".into()))
                .tab_stop(false)
                .view(cx, &t),
        )
        .build()
}

/// R15-A: a refused toggle that is not a tab stop still answers a click
/// with its reason.
#[test]
fn a_refused_toggle_off_the_tab_order_still_says_why_on_a_click() {
    let mut h = harness((80, 24), Mount::Page(refused_toggle_page));
    h.turns(2);
    h.click_text("Feature");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Only an admin can change this.")
    );
}

thread_local! {
    static SEL: Cell<Option<Signal<Option<String>>>> = const { Cell::new(None) };
}

/// A page that rebuilds its table when the selection changes (Providers'
/// engine section does), with a Link per row.
fn rebuilding_table_page(_ctx: &ui::Ctx, cx: Scope) -> View {
    let sel = cx.signal(Some("r0".to_string()));
    SEL.with(|s| s.set(Some(sel)));
    dyn_view_scoped(LayoutStyle::column().grow(1.0), move |rcx| {
        let t = use_theme(rcx).get().tokens;
        let _ = sel.get(); // the whole region rebuilds on a new selection
        let rows = (0..5)
            .map(|i| {
                w::Row::new(
                    format!("r{i}"),
                    vec![
                        w::Cell::Text(vec![w::Ink::new(format!("engine {i}"), t.text)]),
                        w::Cell::Link {
                            label: format!("Learn more {i}"),
                            action: "learn",
                            tip: None,
                        },
                    ],
                )
            })
            .collect();
        w::DataTable::new(
            vec![
                w::Col::new("Engine", w::ColW::Flex { weight: 1, min: 8 }),
                w::Col::new("Docs", w::ColW::Fit { min: 12, max: 16 }),
            ],
            rows,
            sel,
        )
        .view(rcx, &t)
    })
}

/// R15-A (2): a Link click on a row that is not selected keeps the
/// keyboard in the table, even when the selection rebuilds the page.
#[test]
fn a_link_click_on_another_row_keeps_the_keyboard_in_the_table() {
    let mut h = harness((80, 24), Mount::Page(rebuilding_table_page));
    h.turns(2);
    h.click_text("Learn more 2");
    let sel = SEL.with(|s| s.get()).unwrap();
    h.turns(2);
    assert_eq!(
        sel.get_untracked().as_deref(),
        Some("r2"),
        "the click selects its row"
    );
    h.key(b"\x1b[B"); // ↓
    h.turns(2);
    assert_eq!(
        sel.get_untracked().as_deref(),
        Some("r3"),
        "↓ moves the selection: the table kept the focus"
    );
}
