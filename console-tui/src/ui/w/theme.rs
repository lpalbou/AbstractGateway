//! The console themes (DESIGN-TUI.md §5): `gateway-dark` and
//! `gateway-light`, built from the web console's own palettes
//! (`console_themes.py` default dark + `theme-light`) over the engine's
//! house pair (`abstract-dark` / `abstract-light`) for every token the
//! web does not define, registered through the engine's contrast audit.

use abstracttui::prelude::*;
use abstracttui::theme::{self, derive, RegisterMode, ThemeCandidate};

pub const DARK: &str = "gateway-dark";
pub const LIGHT: &str = "gateway-light";

fn hex(h: u32) -> Rgba {
    Rgba::rgb((h >> 16) as u8, (h >> 8) as u8, h as u8)
}

/// The web palette anchors: (bg, surface, raised, accent, ok, warn, error, info).
const WEB_DARK: [u32; 8] = [
    0x1a1a2e, 0x16213e, 0x0f3460, 0xe94560, 0x27ae60, 0xf39c12, 0xe74c3c, 0x60a5fa,
];
const WEB_LIGHT: [u32; 8] = [
    0xf7f7fb, 0xffffff, 0xe6e8f0, 0xe94560, 0x12883e, 0xb16105, 0xdc2626, 0x2563eb,
];

fn candidate(id: &str, label: &str, dark: bool) -> ThemeCandidate {
    let base = theme::registry::get(if dark { "abstract-dark" } else { "abstract-light" })
        .expect("house palette");
    let mut t = base.tokens;
    let a = if dark { WEB_DARK } else { WEB_LIGHT };
    t.bg = hex(a[0]);
    t.surface = hex(a[1]);
    t.surface_raised = hex(a[2]);
    t.accent = hex(a[3]);
    t.ok = hex(a[4]);
    t.warn = hex(a[5]);
    t.error = hex(a[6]);
    t.info = hex(a[7]);
    if !dark {
        t.text = hex(0x0f172a);
        t.text_muted = hex(0x475569);
        // The web accent on white is ~3.6:1 — darken it until text-grade.
        t.accent = derive::mix_until_contrast(t.accent, hex(0x000000), t.surface, 0.0, 0.05, 4.6);
    }
    t.border_focus = t.accent;
    t.cursor = t.accent;
    t.link = t.info;
    t.selection_bg = derive::mix(t.surface, t.accent, if dark { 0.45 } else { 0.22 });
    t.selection_fg = t.text;
    ThemeCandidate {
        id: id.into(),
        label: label.into(),
        dark,
        tokens: t,
    }
}

/// Register both themes (idempotent). A strict-audit refusal falls back
/// to a labelled registration whose warnings are returned — surfaced by
/// the caller, never swallowed.
pub fn register() -> Vec<String> {
    let mut warnings = Vec::new();
    for (id, label, dark) in [(DARK, "Gateway dark", true), (LIGHT, "Gateway light", false)] {
        if theme::registry::get(id).is_some() {
            continue;
        }
        match theme::register(candidate(id, label, dark), RegisterMode::Strict) {
            Ok(_) => {}
            Err(_) => {
                if let Ok(r) = theme::register(candidate(id, label, dark), RegisterMode::Labeled) {
                    warnings.extend(r.warnings);
                }
            }
        }
    }
    warnings
}

/// Flip between the two console themes (the header ☾/☼ button, Ctrl+T).
/// From any other theme, dark ones go to light and light ones to dark.
pub fn flip() {
    register();
    let dark = abstracttui::app::current_theme().dark;
    abstracttui::app::set_theme_by_id(if dark { LIGHT } else { DARK });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_themes_pass_the_strict_audit() {
        for (id, dark) in [(DARK, true), (LIGHT, false)] {
            let c = candidate(id, id, dark);
            let r = theme::register(c, RegisterMode::Strict);
            assert!(r.is_ok(), "{id} refused by the strict audit: {:?}", r.err());
        }
        assert!(register().is_empty());
        assert!(theme::registry::get(DARK).expect("dark").dark);
        assert!(!theme::registry::get(LIGHT).expect("light").dark);
    }
}
