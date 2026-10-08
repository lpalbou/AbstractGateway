//! R15 (adversary S1): every sentence that sends the person to another
//! screen names that screen's REAL accelerator. "configure it on Multimodal
//! (9)" survived from 0.13.1 while 9 had become Models; this test reads
//! every string literal under src/ and checks both forms —
//! "<Screen> (<key>)" and "<key> <Screen>" — against the shell's table
//! (`ui::SCREENS` + `ui::screen_key`). A wrong key is RED; so is a run that
//! finds no reference at all (the scan must see the sources).

use std::path::Path;

use abstractgateway_console::ui;

/// (screen name, its key) for every screen that has an accelerator.
fn table() -> Vec<(&'static str, char)> {
    (0..ui::SCREENS.len())
        .filter_map(|i| ui::screen_key(i).map(|k| (ui::SCREENS[i], k)))
        .collect()
}

/// Every Rust source under `dir`.
fn sources(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for e in std::fs::read_dir(dir).expect("src dir") {
        let p = e.expect("entry").path();
        if p.is_dir() {
            sources(&p, out);
        } else if p.extension().and_then(|x| x.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// The string literals of `src` (plain "…" literals, escapes skipped).
fn literals(src: &str) -> Vec<String> {
    let mut out = Vec::new();
    let b: Vec<char> = src.chars().collect();
    let mut i = 0;
    while i < b.len() {
        // Skip line comments (doc text may cite old keys on purpose).
        if b[i] == '/' && b.get(i + 1) == Some(&'/') {
            while i < b.len() && b[i] != '\n' {
                i += 1;
            }
            continue;
        }
        if b[i] == '\'' {
            // A char literal like '"' must not open a string.
            if b.get(i + 2) == Some(&'\'') {
                i += 3;
                continue;
            }
            if b.get(i + 1) == Some(&'\\') && b.get(i + 3) == Some(&'\'') {
                i += 4;
                continue;
            }
        }
        if b[i] == '"' {
            let mut s = String::new();
            i += 1;
            while i < b.len() && b[i] != '"' {
                if b[i] == '\\' {
                    i += 2;
                    continue;
                }
                s.push(b[i]);
                i += 1;
            }
            out.push(s);
        }
        i += 1;
    }
    out
}

/// Each (screen, key-as-written, where) the literal names.
fn references(lit: &str, names: &[(&'static str, char)]) -> Vec<(&'static str, char, usize)> {
    let mut out = Vec::new();
    for (name, _) in names {
        let mut from = 0;
        while let Some(at) = lit[from..].find(name) {
            let start = from + at;
            let end = start + name.len();
            from = end;
            // "<Screen> (<k>)"
            let after: Vec<char> = lit[end..].chars().take(4).collect();
            if after.len() >= 4 && after[0] == ' ' && after[1] == '(' && after[3] == ')' {
                let k = after[2];
                if k.is_ascii_digit() || k.is_ascii_uppercase() {
                    out.push((*name, k, start));
                }
            }
            // "<k> <Screen>" (the key alone, then the name)
            let before: Vec<char> = lit[..start].chars().rev().take(3).collect();
            if before.len() >= 2 && before[0] == ' ' {
                let k = before[1];
                let alone = before.get(2).is_none_or(|c| !c.is_alphanumeric());
                if alone && (k.is_ascii_digit() || matches!(k, 'H' | 'T' | 'N' | 'S' | 'I')) {
                    out.push((*name, k, start));
                }
            }
        }
    }
    out
}

#[test]
fn every_cross_screen_key_matches_the_shell_table() {
    let names = table();
    let mut files = Vec::new();
    sources(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut files,
    );
    let mut seen = 0;
    let mut wrong = Vec::new();
    for f in files {
        let src = std::fs::read_to_string(&f).expect("read");
        for lit in literals(&src) {
            for (name, k, at) in references(&lit, &names) {
                seen += 1;
                let want = names.iter().find(|(n, _)| *n == name).map(|(_, k)| *k);
                if want != Some(k) {
                    let from = lit[..at]
                        .char_indices()
                        .rev()
                        .nth(12)
                        .map(|(i, _)| i)
                        .unwrap_or(0);
                    let to = (at + name.len() + 4).min(lit.len());
                    let to = (to..=lit.len())
                        .find(|i| lit.is_char_boundary(*i))
                        .unwrap_or(lit.len());
                    wrong.push(format!(
                        "{}: \"…{}…\" names {name} with key {k}; the shell's key is {}",
                        f.file_name().unwrap().to_string_lossy(),
                        &lit[from..to],
                        want.map(String::from).unwrap_or_else(|| "none".into())
                    ));
                }
            }
        }
    }
    assert!(seen > 0, "the scan found no cross-screen reference at all");
    assert!(
        wrong.is_empty(),
        "wrong accelerators:\n{}",
        wrong.join("\n")
    );
}

#[test]
fn the_sandbox_names_multimodal_with_its_real_key() {
    let k = ui::screen_key(ui::SCREEN_ROUTES).unwrap();
    assert_eq!(ui::sandbox::multimodal_ref(), format!("Multimodal ({k})"));
}
