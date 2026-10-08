//! The action glyph table (DESIGN-TUI.md §4.2). Every glyph is one cell
//! wide, East-Asian-width neutral (or ASCII) and absent from Unicode
//! emoji-data, so no terminal promotes it to a double-width emoji. The
//! test below is the authority: a glyph not in this table is refused.

/// (action id, glyph, ascii fallback).
pub const GLYPHS: &[(&str, &str, &str)] = &[
    ("email", "@", "@"),
    ("openai", "⇄", "A"),
    ("logs", "≣", "L"),
    ("workspaces", "◫", "W"),
    ("preferences", "⊜", "P"),
    ("manage", "⬖", "M"),
    ("rotate", "↻", "R"),
    ("archive", "⊟", "x"),
    ("unarchive", "⤒", "u"),
    ("runtime", "▸", ">"),
    ("open", "⇗", ">"),
    ("install", "⤓", "v"),
    ("export", "⤓", "v"),
    ("update", "⇡", "^"),
    ("settings", "⊛", "*"),
    ("edit", "✎", "e"),
    ("clear", "⌀", "0"),
    ("configure", "⊞", "+"),
    ("delete", "⌫", "X"),
    ("copy", "⧉", "c"),
    ("docs", "✦", "?"),
];

/// The glyph of action `id` (ASCII when `utf8` is false). Panics on an
/// unknown id in debug builds — a typo must not render an empty button.
pub fn glyph(id: &str, utf8: bool) -> &'static str {
    match GLYPHS.iter().find(|(k, _, _)| *k == id) {
        Some((_, g, a)) => {
            if utf8 {
                g
            } else {
                a
            }
        }
        None => {
            debug_assert!(false, "no glyph for action {id}");
            "?"
        }
    }
}

/// Is `g` one of the table's glyphs (used by the RowActions builder)?
pub fn is_known(g: &str) -> bool {
    GLYPHS.iter().any(|(_, x, a)| *x == g || *a == g)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Emoji-class code points the table must never contain (the
    /// rejected candidates from the design, plus the classic traps).
    const EMOJI_TRAPS: &[char] = &['⚙', '↗', '☀', '▶', '◼', '⏏', '⤴', '✏', '✉', '⌨'];
    /// Ambiguous-width candidates rejected by the design.
    const AMBIGUOUS: &[char] = &['≡', '▤', '◆', '↓', '±', '◇', '⊕', '⊙', '⇧'];
    /// Measured double-width by the engine (trigrams): the design's first
    /// Preferences candidate.
    const ENGINE_WIDE: &[char] = &['☰', '⚌'];

    #[test]
    fn every_glyph_is_one_cell_and_not_emoji_or_ambiguous() {
        for (id, g, a) in GLYPHS {
            assert_eq!(abstracttui::text::width(g), 1, "{id}: {g} must be 1 cell");
            assert_eq!(abstracttui::text::width(a), 1, "{id}: {a} must be 1 cell");
            assert!(a.is_ascii(), "{id}: fallback {a} must be ASCII");
            for c in g.chars() {
                assert!(!EMOJI_TRAPS.contains(&c), "{id}: {c} is emoji-class");
                assert!(!AMBIGUOUS.contains(&c), "{id}: {c} is ambiguous-width");
                assert!(!ENGINE_WIDE.contains(&c), "{id}: {c} is wide in the engine");
                assert!(
                    !('\u{1F000}'..='\u{1FAFF}').contains(&c),
                    "{id}: {c} is in the emoji planes"
                );
            }
        }
    }

    #[test]
    fn lookup_and_fallback() {
        assert_eq!(glyph("archive", true), "⊟");
        assert_eq!(glyph("archive", false), "x");
        assert!(is_known("⊟"));
        assert!(!is_known("⚙"));
    }
}
