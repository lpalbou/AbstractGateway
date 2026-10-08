//! R15 (DESIGN-TUI.md §2.1): every shell control answers a synthesized
//! mouse click through the real input pipeline — the rail (wide) and the
//! strip with its ‹ › (narrow), the header's resources widget, identity,
//! ✦ Docs and the ☾/☼ appearance switch (in the status bar under 90
//! columns), plus Ctrl+T and the `?` keys panel.

mod r8w4;

use abstractgateway_console::ui::{self, w};
use r8w4::{harness, Harness, Mount};

fn root(size: (i32, i32)) -> Harness {
    w::theme::register();
    let mut h = harness(size, Mount::Root);
    abstracttui::app::set_theme_by_id("gateway-dark");
    h.admin();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(3);
    h
}

fn dark() -> bool {
    abstracttui::app::current_theme().dark
}

/// Click the first `needle` on screen row `y` (0-based).
fn click_on_row(h: &mut Harness, y: usize, needle: &str) -> String {
    let s = h.turns(1);
    let line = s
        .lines()
        .nth(y)
        .unwrap_or_else(|| panic!("no row {y}:\n{s}"));
    let b = line
        .find(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on row {y}:\n{s}"));
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn last_row(h: &mut Harness) -> usize {
    h.turns(1).lines().count() - 1
}

#[test]
fn wide_rail_items_switch_screens_on_click() {
    let mut h = root((120, 40));
    for (label, screen) in [
        ("Apps", ui::SCREEN_APPS),
        ("Runtimes", ui::SCREEN_RUNTIMES),
        ("Network", ui::SCREEN_NETWORK),
        ("Accounts", ui::SCREEN_USERS),
        ("Connection", ui::SCREEN_CONNECTION),
    ] {
        let s = h.turns(1);
        // The rail is the left column: find the label left of the rail's │.
        let (y, line) = s
            .lines()
            .enumerate()
            .skip(1)
            .find(|(_, l)| {
                l.split('│')
                    .next()
                    .is_some_and(|r| r.trim_start().starts_with(label))
            })
            .unwrap_or_else(|| panic!("{label} not in the rail:\n{s}"));
        let x = line[..line.find(label).unwrap()].chars().count() + 1;
        h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
        h.turns(2);
        assert_eq!(h.ui.screen.get_untracked(), screen, "rail click {label}");
    }
}

#[test]
fn narrow_strip_tabs_and_arrows_switch_screens_on_click() {
    let mut h = root((80, 24));
    click_on_row(&mut h, 1, "Workflows");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_WORKFLOWS);
    click_on_row(&mut h, 1, "›");
    assert_ne!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_WORKFLOWS,
        "› moves on"
    );
    let after = h.ui.screen.get_untracked();
    click_on_row(&mut h, 1, "‹");
    assert_ne!(h.ui.screen.get_untracked(), after, "‹ moves back");
}

#[test]
fn header_identity_and_resources_open_their_screens() {
    let mut h = root((120, 40));
    click_on_row(&mut h, 0, "admin@default");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_CONNECTION);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    click_on_row(&mut h, 0, "Mem");
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_MODELS,
        "the resources widget opens Resources"
    );
}

#[test]
fn header_docs_button_opens_the_docs_assistant() {
    let mut h = root((120, 40));
    let before = h.turns(1);
    let s = click_on_row(&mut h, 0, "✦ Docs");
    let s = if s == before { h.turns(3) } else { s };
    assert_ne!(s, before, "✦ Docs opened something:\n{s}");
    assert!(
        s.contains("Docs") || s.contains("docs"),
        "the docs assistant:\n{s}"
    );
}

#[test]
fn theme_switch_is_one_click_in_the_header_and_in_the_narrow_status_bar() {
    let mut h = root((120, 40));
    assert!(dark());
    click_on_row(&mut h, 0, "☾");
    assert!(!dark(), "header ☾ → light");
    let s = h.turns(2);
    assert!(
        s.lines().next().unwrap().contains('☼'),
        "the switch shows the light glyph:\n{s}"
    );
    click_on_row(&mut h, 0, "☼");
    assert!(dark(), "header ☼ → dark");

    let mut h = root((80, 24));
    let y = last_row(&mut h);
    // The status bar carries it whatever the header has room for.
    click_on_row(&mut h, y, "☾");
    assert!(!dark(), "status bar ☾ → light");
    // Ctrl+T flips back.
    h.key(b"\x14");
    h.turns(2);
    assert!(dark(), "Ctrl+T → dark");
}

#[test]
fn question_mark_opens_the_keys_panel_and_its_close_button_closes_it() {
    let mut h = root((120, 40));
    let s = h.key(b"?");
    let s = if s.contains("Keys — Accounts") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Keys — Accounts"), "keys panel:\n{s}");
    assert!(
        s.contains("Everything here also works with the mouse"),
        "{s}"
    );
    let s = h.click_text(" Close ");
    let s = if s.contains("Keys — ") {
        h.turns(2)
    } else {
        s
    };
    assert!(!s.contains("Keys — "), "Close closed it:\n{s}");
}

/// R15 FC1: no key-only control in the shell — the status bar's `?` opens
/// the keys panel by mouse, and the panel quits by mouse.
#[test]
fn the_keys_panel_and_quit_are_one_click_away() {
    let mut h = root((120, 40));
    let y = last_row(&mut h);
    let s = h.turns(1);
    let last = s.lines().nth(y).unwrap();
    let b = last.rfind('?').expect("a ? button in the status bar");
    let x = last[..b].chars().count() + 1;
    let s = h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    let s = if s.contains("Keys — ") {
        s
    } else {
        h.turns(2)
    };
    assert!(
        s.contains("Keys — Accounts"),
        "? opens the keys panel:\n{s}"
    );
    assert!(s.contains("Quit the console"), "{s}");
}

/// R15 FC1: the setup guide's keys are buttons — Next ›, ‹ Back, Steps….
#[test]
fn the_setup_guide_walks_by_mouse() {
    let mut h = root((120, 40));
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_CONNECTION);
    let s = h.turns(3);
    assert!(
        s.contains("Next ›") && s.contains("‹ Back") && s.contains("Steps…"),
        "{s}"
    );
    h.click_text("Next ›");
    h.turns(2);
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::WIZARD_STEPS[1],
        "Next › walks to the second step"
    );
    h.click_text("‹ Back");
    h.turns(2);
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_CONNECTION,
        "‹ Back returns"
    );
    let s = h.click_text("Steps…");
    let s = if s.contains("Setup guide") {
        s
    } else {
        h.turns(2)
    };
    assert!(
        s.contains("Setup guide"),
        "Steps… opens the guide menu:\n{s}"
    );
}

/// R15 FC1 meta: every Ctrl chord a screen names in its words has a button
/// that does the same, proven by a click test. A new key-only chord in any
/// `src/ui` string fails here until it gets its control (and its test).
#[test]
fn every_ctrl_chord_named_in_the_ui_has_a_clickable_control() {
    // chord letter → the click test that proves its control
    let proven: &[(char, &str)] = &[
        ('N', "the_setup_guide_walks_by_mouse (Next ›)"),
        ('P', "the_setup_guide_walks_by_mouse (‹ Back)"),
        (
            'G',
            "the_setup_guide_walks_by_mouse (Steps…) + Setup's Setup guide button",
        ),
        (
            'T',
            "theme_switch_is_one_click_in_the_header_and_in_the_narrow_status_bar",
        ),
        (
            'C',
            "the_keys_panel_and_quit_are_one_click_away (Quit the console)",
        ),
        ('O', "headless_ui my_email_different_login_is_a_link_…"),
        (
            'U',
            "headless_ui my_email_imap_tab_is_first_default_… (Use a different account)",
        ),
    ];
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/src/ui");
    let mut found = std::collections::BTreeMap::<char, String>::new();
    let mut stack = vec![std::path::PathBuf::from(dir)];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                stack.push(p);
                continue;
            }
            if p.extension().and_then(|x| x.to_str()) != Some("rs") {
                continue;
            }
            let src = std::fs::read_to_string(&p).unwrap();
            for line in src.lines() {
                let code = line.split("//").next().unwrap_or("");
                // Only string literals: the text between quotes.
                for (i, seg) in code.split('"').enumerate() {
                    if i % 2 == 0 {
                        continue;
                    }
                    let mut rest = seg;
                    while let Some(k) = rest.find("Ctrl+") {
                        if let Some(c) = rest[k + 5..].chars().next() {
                            if c.is_ascii_uppercase() {
                                found
                                    .entry(c)
                                    .or_insert_with(|| format!("{}: {seg}", p.display()));
                            }
                        }
                        rest = &rest[k + 5..];
                    }
                }
            }
        }
    }
    assert!(
        found.contains_key(&'O') && found.contains_key(&'N'),
        "the scan reads the sources: {found:?}"
    );
    for (c, at) in &found {
        assert!(
            proven.iter().any(|(k, _)| k == c),
            "Ctrl+{c} is named in the UI with no proven clickable control: {at}"
        );
    }
}
