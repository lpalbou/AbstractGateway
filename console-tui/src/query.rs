//! THE CONSOLE QUERY LANGUAGE — one rule, every search box.
//!
//! Transcribed character-for-character from the gateway's
//! `_glob_matches` / `_query_value_matches`
//! (`src/abstractgateway/routes/gateway.py`) and the web console's JS
//! copy. The Runs and Artifacts tabs filter SERVER-side and the Cache and
//! Logs tabs filter here, so the same typed query has to mean the same
//! thing in three languages; all three copies must stay behaviourally
//! identical.
//!
//! ## The defect this replaces
//!
//! Operator 2026-08-20: typing `*.jpg` in a runtime tab's search box
//! found nothing. Every filter was a plain substring, so the `*` was
//! matched literally and no row could ever carry it.
//!
//! ## The rule
//!
//! * no `*` and no `?` — case-insensitive SUBSTRING, exactly as before
//!   (`06-13` still finds a June 13th row, `abc` still finds `xabcx`)
//! * any `*` or `?` — case-insensitive GLOB anchored to the WHOLE value,
//!   then retried against the value's basename
//!
//! `*` crosses `/` on purpose: these boxes filter a FLAT list of rows,
//! they do not walk a tree, so `*.jpg` must find
//! `/data/runs/r1/out/photo.jpg`. The basename pass is what makes an
//! anchored pattern usable against a stored absolute path —
//! `photo?.jpg` finds `/data/runs/r1/photo7.jpg`.
//!
//! `[` is LITERAL. Character classes are not worth a three-way drift bug
//! in a filename filter.

/// A prepared query: lowercased once, wildcard-classified once.
///
/// Built per FILTER call, never per row — `is_glob` and the lowercase
/// fold depend on the query alone, and these filters run across every
/// field of every row. Owning the lowercased text here also makes the
/// "needle must already be folded" contract unforgeable; call sites hand
/// over exactly what the user typed.
#[derive(Debug, Clone)]
pub struct Needle {
    text: String,
    glob: bool,
}

impl Needle {
    pub fn new(query: &str) -> Self {
        let text = query.trim().to_lowercase();
        // `[` is LITERAL — only `*` and `?` turn a query into a glob.
        let glob = text.contains('*') || text.contains('?');
        Self { text, glob }
    }

    /// An empty query filters nothing out.
    pub fn is_empty(&self) -> bool {
        self.text.is_empty()
    }

    /// True when this query is being read as a glob rather than a
    /// substring — the note lines say so, so the user can tell which
    /// half of the language answered.
    pub fn is_glob(&self) -> bool {
        self.glob
    }

    /// One candidate field.
    pub fn matches(&self, value: &str) -> bool {
        if self.text.is_empty() {
            return true;
        }
        if value.is_empty() {
            // An ABSENT field never matches — not even `*`.
            return false;
        }
        let text = value.to_lowercase();
        if !self.glob {
            return text.contains(&self.text);
        }
        if glob_matches(&text, &self.text) {
            return true;
        }
        let base = text.rsplit(['/', '\\']).next().unwrap_or(&text);
        base != text && glob_matches(base, &self.text)
    }

    /// Any of a row's fields — the row-level OR.
    pub fn matches_any<'a>(&self, values: impl IntoIterator<Item = &'a str>) -> bool {
        if self.text.is_empty() {
            return true;
        }
        values.into_iter().any(|v| self.matches(v))
    }
}

/// Anchored `*`/`?` glob. Linear scan with one backtrack point.
///
/// Walks CHARS, not bytes: one `?` consumes one character, so a name with
/// accents or CJK counts the way the user sees it.
fn glob_matches(value: &str, pattern: &str) -> bool {
    let v: Vec<char> = value.chars().collect();
    let p: Vec<char> = pattern.chars().collect();
    let (mut vi, mut pi) = (0usize, 0usize);
    // `star` is the pattern index of the last `*`; `mark` is how far it
    // has swallowed. `None` = no `*` seen yet, so a mismatch is final.
    let mut star: Option<usize> = None;
    let mut mark = 0usize;
    while vi < v.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == v[vi]) {
            vi += 1;
            pi += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some(pi);
            pi += 1;
            mark = vi;
        } else if let Some(s) = star {
            // The last `*` swallows one more character and we retry.
            pi = s + 1;
            mark += 1;
            vi = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(value: &str, query: &str) -> bool {
        Needle::new(query).matches(value)
    }

    #[test]
    fn plain_queries_stay_substrings() {
        // The behaviour every existing filter had, unchanged.
        assert!(m("2026-06-13T09:00:00", "06-13"));
        assert!(m("xabcx", "abc"));
        assert!(!m("xabcx", "abd"));
        assert!(!Needle::new("06-13").is_glob());
    }

    #[test]
    fn a_filetype_glob_finds_the_file() {
        // The operator's case, on a bare name and on a stored path.
        assert!(Needle::new("*.jpg").is_glob());
        assert!(m("photo.jpg", "*.jpg"));
        assert!(m("/data/runs/r1/out/photo.jpg", "*.jpg"));
        assert!(!m("photo.png", "*.jpg"));
    }

    #[test]
    fn the_basename_pass_rescues_anchored_patterns() {
        // `photo?.jpg` cannot match a value starting with `/`; the
        // basename retry is the whole point of that second pass.
        assert!(m("/data/runs/r1/photo7.jpg", "photo?.jpg"));
        assert!(!m("/data/runs/r1/photo77.jpg", "photo?.jpg"));
        assert!(m("photo7.jpg", "photo?.jpg"));
        assert!(m("C:\\logs\\gateway.log", "gateway.*"));
    }

    #[test]
    fn globs_are_anchored_and_star_crosses_slashes() {
        assert!(m("run-abc-123", "run-*"));
        assert!(!m("x-run-abc", "run-*"));
        assert!(m("a/b/c.log", "a*c.log"));
        assert!(m("anything", "*"));
    }

    #[test]
    fn case_is_ignored_on_both_halves() {
        assert!(m("PHOTO.JPG", "*.jpg"));
        assert!(m("photo.jpg", "*.JPG"));
        // Non-ASCII folds too — the reason this is `to_lowercase`, not
        // `to_ascii_lowercase`, on BOTH sides.
        assert!(m("CAFÉ.jpg", "caf\u{e9}*"));
    }

    #[test]
    fn an_absent_field_never_matches_but_an_absent_query_keeps_every_row() {
        assert!(!m("", "*"));
        assert!(Needle::new("").is_empty());
        assert!(m("", ""));
        assert!(Needle::new("   ").is_empty());
    }

    #[test]
    fn multibyte_names_count_one_question_mark_per_character() {
        // A byte-wise matcher would need three `?` for `é`.
        assert!(m("café.jpg", "caf?.jpg"));
        assert!(m("日本.log", "??.log"));
    }

    #[test]
    fn backtracking_terminates_on_the_pathological_pattern() {
        // The classic `a*a*a*…` blowup: this must ANSWER, not hang.
        assert!(!m(&"a".repeat(64), "a*a*a*a*a*b"));
        assert!(m(&"a".repeat(64), "a*a*a*a*a*a"));
    }

    /// A naive, obviously-correct recursive matcher. Exists ONLY to be
    /// disagreed with: `glob_matches` is the iterative one-backtrack-point
    /// version, and that optimization is exactly where such matchers go
    /// wrong. The Python original is fuzzed against `fnmatch` the same way.
    fn reference_matches(v: &[char], p: &[char]) -> bool {
        if p.is_empty() {
            return v.is_empty();
        }
        match p[0] {
            '*' => {
                reference_matches(v, &p[1..])
                    || (!v.is_empty() && reference_matches(&v[1..], p))
            }
            '?' => !v.is_empty() && reference_matches(&v[1..], &p[1..]),
            c => !v.is_empty() && v[0] == c && reference_matches(&v[1..], &p[1..]),
        }
    }

    #[test]
    fn the_iterative_matcher_agrees_with_a_naive_recursive_one() {
        // Deterministic LCG — no dev-dependency for 20k cases.
        let mut seed: u64 = 0x2026_0820;
        let mut next = move || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 33) as usize
        };
        let alphabet: Vec<char> = "ab./".chars().collect();
        let pat_alphabet: Vec<char> = "ab./*?".chars().collect();
        for _ in 0..20_000 {
            let vlen = next() % 8;
            let plen = next() % 6;
            let v: Vec<char> = (0..vlen).map(|_| alphabet[next() % alphabet.len()]).collect();
            let p: Vec<char> = (0..plen)
                .map(|_| pat_alphabet[next() % pat_alphabet.len()])
                .collect();
            let (vs, ps): (String, String) = (v.iter().collect(), p.iter().collect());
            assert_eq!(
                glob_matches(&vs, &ps),
                reference_matches(&v, &p),
                "value {vs:?} pattern {ps:?}"
            );
        }
    }

    #[test]
    fn matches_any_is_the_row_level_or() {
        let row = ["run-7", "", "/logs/gateway.jpg"];
        assert!(Needle::new("*.jpg").matches_any(row.iter().copied()));
        assert!(!Needle::new("*.png").matches_any(row.iter().copied()));
        // An empty query keeps the row even when every field is empty.
        assert!(Needle::new("").matches_any(["", ""]));
    }
}
