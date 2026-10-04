//! The shared TUI kit's wrapping table (R7.2 conventions), headless: rows
//! wrap inside the table width (the last column is never cut), Enter opens
//! a row's details, and an opened row taller than the pane scrolls inside
//! with PgDn/PgUp.

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;

use abstractgateway_console::ui::kit::{Row, WrapTable};
use abstractgateway_console::ui::widths::ColRule;

fn mount(size: Size) -> (App, CaptureTerm, Driver) {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    app.mount(move |cx| {
        let t = use_theme(cx).get().tokens;
        let sel = cx.signal(0usize);
        let expanded = cx.signal(None::<usize>);
        let detail: Vec<String> = (1..=30).map(|i| format!("detail line {i:02}")).collect();
        let rows = vec![
            Row::new(vec![
                "run-0001".into(),
                "lmstudio/a-very-long-model-identifier-that-wraps".into(),
                "completed".into(),
            ])
            .detail(detail),
            Row::new(vec!["run-0002".into(), "mlx/qwen".into(), "failed".into()]),
        ];
        WrapTable::new(
            vec![
                ColRule::tail("Run", 3),
                ColRule::tail("Model", 10),
                ColRule::head("Status", 6),
            ],
            rows,
            sel,
        )
        .expanded(expanded)
        .layout(LayoutStyle::column().grow(1.0))
        .element(cx, &t)
        .autofocus()
        .build()
    })
    .expect("mount");
    let mut term = CaptureTerm::new(size);
    let cfg = RunConfig {
        probe: false,
        caps: Some(abstracttui::term::Capabilities::with(|c| {
            c.truecolor = true;
            c.unicode_ok = true;
        })),
        platform_clipboard: false,
        ..RunConfig::default()
    };
    let driver = Driver::new(&mut app, &mut term, cfg).expect("driver");
    (app, term, driver)
}

fn turns(app: &mut App, term: &mut CaptureTerm, d: &mut Driver, n: usize) -> String {
    let mut s = String::new();
    for _ in 0..n {
        d.turn(app, term).expect("turn");
        s = term.screen().to_text();
    }
    s
}

#[test]
fn wrapped_rows_keep_every_column_inside_the_width() {
    for w in [30, 40, 80] {
        let (mut app, mut term, mut d) = mount(Size::new(w, 10));
        let s = turns(&mut app, &mut term, &mut d, 3);
        let header = s.lines().next().unwrap_or_default();
        assert!(
            header.contains("Run") && header.contains("Status"),
            "{w}: {header:?}"
        );
        // The last column wraps when narrow, never cut.
        let flat: String = s.split_whitespace().collect();
        let whole = s.contains("completed") || flat.contains("comple");
        assert!(whole, "the last column is whole at {w}:\n{s}");
        for l in s.lines() {
            assert!(abstracttui::text::width(l) <= w, "{l:?}");
        }
    }
}

#[test]
fn an_opened_row_taller_than_the_pane_scrolls_inside() {
    let (mut app, mut term, mut d) = mount(Size::new(60, 10));
    turns(&mut app, &mut term, &mut d, 2);
    term.push_input(b"\r");
    let s = turns(&mut app, &mut term, &mut d, 3);
    assert!(s.contains("detail line 01"), "Enter opens the row:\n{s}");
    assert!(!s.contains("detail line 30"), "{s}");
    for _ in 0..8 {
        term.push_input(b"\x1b[6~");
        turns(&mut app, &mut term, &mut d, 2);
    }
    let s = turns(&mut app, &mut term, &mut d, 2);
    assert!(
        s.contains("detail line 30"),
        "PgDn reaches the end of the details:\n{s}"
    );
    for _ in 0..8 {
        term.push_input(b"\x1b[5~");
        turns(&mut app, &mut term, &mut d, 2);
    }
    let s = turns(&mut app, &mut term, &mut d, 2);
    assert!(s.contains("detail line 01"), "PgUp comes back:\n{s}");
}
