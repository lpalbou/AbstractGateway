//! Round 18 ("Spoken language", CONTRACT): the account's spoken language on the
//! terminal console, by mouse (CONVERSION-PATTERN: a click for every control +
//! the meta-test, one keyboard path, one hover).
//!
//! - Accounts → Preferences: the row AFTER the time zone — label and help from
//!   the gateway's `spoken_language` block, a Select over ONLY the served
//!   choices (labels verbatim), a pick is ONE PUT `{"spoken_language": <value>}`
//!   at once (no Save), "Saved." / "Not saved. <message>"; an answer without
//!   the block shows the web's seam sentence (never a hidden row).
//! - Multimodal: the same control for the signed-in account (`me`), read from
//!   `GET /accounts/me/preferences` when the screen mounts connected, PUT on
//!   change with a reload, its state line under it.
//!
//! Hermetic over answers recorded from the gateway's own block
//! (tests/fixtures/r14w3_prefs_*.json carry it; r18_spoken_*.json). The web
//! words are tests/fixtures/r15_web_wording_preferences_spoken_language.json
//! (scripts/extract_web_wording.py). Captures land in R8W4_SHOTS_DIR.

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{Loadable, RoutesData};
use abstractgateway_console::ui::account_preferences as prefs;
use abstractgateway_console::ui::{self, routes, users};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};
use serde_json::{json, Value};

const SIZES3: [(i32, i32); 3] = [SIZES[0], SIZES[1], (180, 50)];
const SLOT: &str = "prefs.default.alice";
const WK: &str = "prefs.default.alice.write";
const PATH: &str = "/accounts/alice/preferences";

fn fx(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
}

fn w(k: &str) -> String {
    fx("r15_web_wording_preferences_spoken_language")[k]
        .as_str()
        .expect(k)
        .to_string()
}

fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' ' || c == '┃'))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn label() -> String {
    fx("r14w3_prefs_alice_set")["spoken_language"]["label"]
        .as_str()
        .unwrap()
        .to_string()
}

fn help() -> String {
    fx("r14w3_prefs_alice_set")["spoken_language"]["help"]
        .as_str()
        .unwrap()
        .to_string()
}

fn refused() -> ApiError {
    let body = fx("r18_spoken_refused");
    ApiError {
        kind: ApiErrorKind::Http(400),
        message: body["detail"].to_string(),
        body: Some(body["detail"].clone()),
        timed_out: false,
    }
}

fn refused_sentence() -> String {
    fx("r18_spoken_refused")["detail"]["message"]
        .as_str()
        .unwrap()
        .to_string()
}

/// (method, path, body, reload) of a write sent.
type Put = (String, String, Value, Vec<(String, String)>);

/// Every write sent.
fn puts(cmds: &[Cmd]) -> Vec<Put> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                method,
                path,
                body,
                reload,
                ..
            }) => Some((method.clone(), path.clone(), body.clone(), reload.clone())),
            _ => None,
        })
        .collect()
}

fn gets(cmds: &[Cmd]) -> Vec<(String, String)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) => Some((key.clone(), path.clone())),
            _ => None,
        })
        .collect()
}

fn click_at(h: &mut r8w4::Harness, row: usize, col: usize) -> String {
    let c = format!(
        "\x1b[<0;{};{}M\x1b[<0;{};{}m",
        col + 1,
        row + 1,
        col + 1,
        row + 1
    );
    h.key(c.as_bytes())
}

/// Click the Select trigger on the "Spoken language" line (its current label).
fn open_select(h: &mut r8w4::Harness, current: &str) -> String {
    let lab = label();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            let at = l.find(&lab)?;
            let c = l[at..].find(current)? + at;
            Some((i, l[..c].chars().count()))
        })
        .unwrap_or_else(|| panic!("no {lab:?} line showing {current:?}:\n{s}"));
    click_at(h, row, col + 1);
    h.turns(2)
}

/// Click an option of the open list: a line, right of the trigger's column,
/// whose text (marks trimmed) IS `option`.
fn click_option(h: &mut r8w4::Harness, option: &str) -> String {
    let lab = label();
    let s = h.turns(1);
    let lines: Vec<Vec<char>> = s.lines().map(|l| l.chars().collect()).collect();
    let x0 = lines
        .iter()
        .find_map(|l| {
            let t: String = l.iter().collect();
            let at = t.find(&lab)?;
            Some(t[..at].chars().count() + lab.chars().count())
        })
        .unwrap_or_else(|| panic!("no {lab:?} line:\n{s}"));
    let marks: &[char] = &[' ', '▸', '●', '✓', '│', '▐', '▌', '┃', '>'];
    let (row, col) = lines
        .iter()
        .enumerate()
        .find_map(|(i, l)| {
            (x0..l.len()).find_map(|start| {
                let rest: String = l.iter().skip(start).collect();
                let body = rest.trim_start_matches(marks);
                let lead = rest.chars().count() - body.chars().count();
                // The option alone: what follows it is a gap (the list is drawn
                // over the modal, so text beyond the list's edge may show).
                let lone = body.starts_with(option)
                    && (body[option.len()..].is_empty()
                        || body[option.len()..].starts_with("  ")
                        || body[option.len()..]
                            .trim_end_matches(marks)
                            .trim()
                            .is_empty())
                    && (start == 0 || marks.contains(&l[start - 1]) || l[start - 1] == ' ');
                lone.then_some((i, start + lead))
            })
        })
        .unwrap_or_else(|| panic!("option {option:?} not in the open list:\n{s}"));
    click_at(h, row, col + 1);
    h.turns(2)
}

// ---------------------------------------------------------------- the modal

fn users_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

/// The Accounts page with alice's Preferences modal open on `answer`.
fn modal(size: (i32, i32), answer: Value) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(users_view));
    h.admin();
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&fx("r14w3_accounts_w2")).unwrap(),
    ));
    h.turns(3);
    let idx = h.store.accounts.with_untracked(|d| {
        d.ready()
            .unwrap()
            .iter()
            .filter(|r| !r.archived)
            .position(|r| r.id == "alice")
    });
    h.ui.account_sel.set(idx.expect("alice"));
    h.turns(2);
    h.sent();
    h.key(b"p");
    h.sent();
    h.store.json.set(SLOT, Loadable::Ready(answer));
    h.turns(3);
    h
}

/// Scroll the modal until the spoken-language row shows (a short terminal).
fn reveal(h: &mut r8w4::Harness) -> String {
    let lab = label();
    for _ in 0..12 {
        let s = h.turns(1);
        if s.lines().any(|l| l.contains(&lab)) {
            return s;
        }
        h.wheel_down(1);
    }
    panic!("the {lab:?} row never showed:\n{}", h.turns(1));
}

/// What a pick sends, by class: "auto" or a served "code".
fn offered_classes(answer: &Value) -> BTreeSet<&'static str> {
    let sl = prefs::parse(answer).unwrap().spoken_language.unwrap();
    sl.options()
        .iter()
        .map(|(v, _)| if v == "auto" { "auto" } else { "code" })
        .collect()
}

/// The classes the click tests below drive by mouse.
const CLICKED: &[&str] = &["auto", "code"];

#[test]
fn every_offered_spoken_language_option_has_a_click_test() {
    let mut offered = BTreeSet::new();
    for a in [
        fx("r14w3_prefs_alice_set"),
        fx("r18_spoken_put_fr"),
        fx("r14w3_prefs_me"),
    ] {
        offered.extend(offered_classes(&a));
        // The list IS the served choices, labels verbatim, nothing added.
        let sl = prefs::parse(&a).unwrap().spoken_language.unwrap();
        let served: Vec<(String, String)> = a["spoken_language"]["choices"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                (
                    c["value"].as_str().unwrap().to_string(),
                    c["label"].as_str().unwrap().to_string(),
                )
            })
            .collect();
        assert_eq!(sl.options(), served);
    }
    let clicked: BTreeSet<&str> = CLICKED.iter().copied().collect();
    assert_eq!(
        offered, clicked,
        "an offered option class has no click test"
    );
}

#[test]
fn the_row_reads_like_the_web_after_the_time_zone() {
    assert_eq!(prefs::SPOKEN_SEAM, w("seam"));
    assert_eq!(prefs::SAVED, w("saved"));
    assert_eq!(prefs::SAVED, w("multimodal_saved"));
    assert_eq!(prefs::ME_READ_FAILED, w("multimodal_read_failed_prefix"));
    assert!(prefs::refusal(&refused()).starts_with(&w("not_saved_prefix")));
    assert_eq!(w("not_saved_prefix"), w("multimodal_not_saved_prefix"));
    for size in SIZES3 {
        let mut h = modal(size, fx("r14w3_prefs_alice_set"));
        let s = reveal(&mut h);
        drop(s);
        let s = h.shoot("r18-preferences-spoken-language");
        assert!(
            s.lines()
                .any(|l| l.contains(&label()) && l.contains("Auto (detected)")),
            "{s}"
        );
        h.assert_fits();
    }
    // Wide enough to show every row: the order is apps, time zone, spoken language.
    let mut h = modal((180, 50), fx("r14w3_prefs_alice_set"));
    let s = h.turns(2);
    let at = |needle: &str| {
        s.lines()
            .position(|l| l.contains(needle))
            .unwrap_or_else(|| panic!("{needle:?}:\n{s}"))
    };
    assert!(at("Assistant") < at("Time zone"), "{s}");
    assert!(at("Time zone") < at(&label()), "{s}");
    assert!(flat(&s).contains(&help()), "{s}");
}

#[test]
fn click_pick_french_is_one_put_then_saved() {
    for size in [(120, 40), (180, 50)] {
        let mut h = modal(size, fx("r14w3_prefs_alice_set"));
        reveal(&mut h);
        let s = open_select(&mut h, "Auto (detected)");
        assert!(puts(&h.sent()).is_empty(), "opening sends nothing");
        h.shoot("r18-preferences-spoken-language-open");
        assert!(s.contains("French"), "{s}");
        click_option(&mut h, "French");
        assert_eq!(
            puts(&h.sent()),
            vec![(
                "PUT".to_string(),
                PATH.to_string(),
                json!({"spoken_language": "fr"}),
                vec![(SLOT.to_string(), PATH.to_string())]
            )],
            "{size:?}"
        );
        assert!(flat(&h.turns(1)).contains("Saving…"));
        h.store
            .json
            .set_write(WK, Some(WriteState::Done(fx("r18_spoken_put_fr"))));
        h.store
            .json
            .set(SLOT, Loadable::Ready(fx("r18_spoken_put_fr")));
        h.turns(2);
        let s = reveal(&mut h);
        let s = if s.contains(&w("saved")) {
            s
        } else {
            h.turns(2)
        };
        h.shoot("r18-preferences-spoken-language-saved");
        assert!(flat(&s).contains(&w("saved")), "{s}");
        assert!(
            s.lines()
                .any(|l| l.contains(&label()) && l.contains("French")),
            "the reloaded row shows the stored value:\n{s}"
        );
    }
}

#[test]
fn click_auto_sends_auto() {
    let mut h = modal((180, 50), fx("r18_spoken_put_fr"));
    open_select(&mut h, "French");
    click_option(&mut h, "Auto (detected)");
    assert_eq!(
        puts(&h.sent())
            .into_iter()
            .map(|(m, p, b, _)| (m, p, b))
            .collect::<Vec<_>>(),
        vec![(
            "PUT".to_string(),
            PATH.to_string(),
            json!({"spoken_language": "auto"})
        )]
    );
    h.store
        .json
        .set_write(WK, Some(WriteState::Done(fx("r14w3_prefs_alice_set"))));
    assert!(flat(&h.turns(3)).contains("Saved."));
}

#[test]
fn a_refusal_is_not_saved_then_the_gateways_sentence() {
    let mut h = modal((180, 50), fx("r14w3_prefs_alice_set"));
    open_select(&mut h, "Auto (detected)");
    click_option(&mut h, "German");
    assert_eq!(puts(&h.sent())[0].2, json!({"spoken_language": "de"}));
    h.store
        .json
        .set_write(WK, Some(WriteState::Failed(refused())));
    let s = h.shoot("r18-preferences-spoken-language-refused");
    assert!(
        flat(&s).contains(&format!("Not saved. {}", refused_sentence())),
        "{s}"
    );
}

#[test]
fn a_missing_block_is_the_seam_sentence_never_a_hidden_row() {
    let mut a = fx("r14w3_prefs_alice_set");
    a.as_object_mut().unwrap().remove("spoken_language");
    let mut h = modal((180, 50), a);
    let s = h.shoot("r18-preferences-spoken-language-missing");
    let f = flat(&s);
    assert!(f.contains(&w("seam")), "{s}");
    // The other rows still render: the error sits in the row's place only.
    assert!(
        f.contains("AbstractCode — chat agent") && f.contains("Time zone"),
        "{s}"
    );
}

#[test]
fn without_can_edit_the_value_shows_and_a_click_sends_nothing() {
    let mut a = fx("r18_spoken_put_fr");
    a["can_edit"] = json!(false);
    let mut h = modal((180, 50), a);
    let s = h.turns(1);
    assert!(
        s.lines()
            .any(|l| l.contains(&label()) && l.contains("French")),
        "{s}"
    );
    open_select(&mut h, "French");
    assert!(!flat(&h.turns(1)).contains("German"));
    assert!(puts(&h.sent()).is_empty());
}

#[test]
fn keyboard_tab_to_the_row_down_enter() {
    let mut h = modal((180, 50), fx("r14w3_prefs_alice_set"));
    // Two app Selects (the first autofocused), the time-zone Combobox, then this Select.
    h.key(b"\t");
    h.key(b"\t");
    h.key(b"\t");
    h.key(b"\r");
    h.key(b"\x1b[B");
    h.key(b"\r");
    let sl = prefs::parse(&fx("r14w3_prefs_alice_set"))
        .unwrap()
        .spoken_language
        .unwrap();
    assert_eq!(
        puts(&h.sent())
            .into_iter()
            .map(|(_, p, b, _)| (p, b))
            .collect::<Vec<_>>(),
        vec![(
            PATH.to_string(),
            json!({"spoken_language": sl.choices[1].0})
        )]
    );
}

// ----------------------------------------------------------- the Multimodal

fn routes_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    routes::view(cx, ctx, &t)
}

fn routes_payload() -> Value {
    json!({
        "ok": true, "writable": true,
        "authority": "abstractcore.gateway_runtime", "source": "abstractcore.gateway_runtime",
        "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text", "label": "Text Input",
             "provider": "lmstudio", "model": "test-model-a",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.voice", "kind": "input", "modality": "voice", "label": "Voice Input",
             "provider": "faster-whisper", "model": "large-v3",
             "source": "abstractcore.gateway_runtime", "configured": true}
        ]
    })
}

/// The Multimodal screen, signed in, before the `me` answer arrives.
fn multimodal(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(routes_view));
    h.admin();
    h.store
        .routes
        .set(Loadable::Ready(RoutesData::from_value(&routes_payload())));
    h.turns(3);
    h
}

fn me_ready(h: &mut r8w4::Harness, answer: Value) -> String {
    h.store.json.set(prefs::ME_SLOT, Loadable::Ready(answer));
    h.turns(3)
}

#[test]
fn the_multimodal_screen_reads_me_when_it_mounts_connected() {
    let mut h = multimodal((120, 40));
    let g = gets(&h.sent());
    assert!(
        g.contains(&(prefs::ME_SLOT.to_string(), prefs::ME_PATH.to_string())),
        "{g:?}"
    );
    assert_eq!(prefs::ME_PATH, "/accounts/me/preferences");
    // Nothing shows while the read is in flight (no placeholder control).
    assert!(!h.turns(1).contains(&label()));
}

#[test]
fn the_multimodal_line_sits_under_transcription_and_reads_like_the_web() {
    for size in SIZES3 {
        let mut h = multimodal(size);
        me_ready(&mut h, fx("r14w3_prefs_me"));
        let s = h.shoot("r18-multimodal-spoken-language");
        let at = |needle: &str| {
            s.lines()
                .position(|l| l.contains(needle))
                .unwrap_or_else(|| panic!("{needle:?}:\n{s}"))
        };
        assert!(at("Transcription") < at(&label()), "{s}");
        assert!(
            s.lines()
                .any(|l| l.contains(&label()) && l.contains("Auto (detected)")),
            "{s}"
        );
        h.assert_fits();
    }
}

#[test]
fn multimodal_click_pick_french_is_one_put_on_me_then_saved() {
    let mut h = multimodal((120, 40));
    me_ready(&mut h, fx("r14w3_prefs_me"));
    h.sent();
    open_select(&mut h, "Auto (detected)");
    h.shoot("r18-multimodal-spoken-language-open");
    click_option(&mut h, "French");
    assert_eq!(
        puts(&h.sent()),
        vec![(
            "PUT".to_string(),
            prefs::ME_PATH.to_string(),
            json!({"spoken_language": "fr"}),
            vec![(prefs::ME_SLOT.to_string(), prefs::ME_PATH.to_string())]
        )]
    );
    assert!(flat(&h.turns(1)).contains("Saving…"));
    h.store.json.set_write(
        prefs::ME_WRITE,
        Some(WriteState::Done(fx("r18_spoken_put_fr"))),
    );
    me_ready(&mut h, fx("r18_spoken_put_fr"));
    let s = h.shoot("r18-multimodal-spoken-language-saved");
    assert!(flat(&s).contains(&w("multimodal_saved")), "{s}");
    assert!(
        s.lines()
            .any(|l| l.contains(&label()) && l.contains("French")),
        "{s}"
    );
}

#[test]
fn multimodal_after_a_restart_shows_the_stored_value() {
    // A fresh console (new harness = a restart) reads `me` again and shows French.
    let mut h = multimodal((120, 40));
    assert!(gets(&h.sent()).iter().any(|(k, _)| k == prefs::ME_SLOT));
    let s = me_ready(&mut h, fx("r18_spoken_put_fr"));
    assert!(
        s.lines()
            .any(|l| l.contains(&label()) && l.contains("French")),
        "{s}"
    );
}

#[test]
fn multimodal_refusal_and_read_failure_and_seam_are_said() {
    let mut h = multimodal((120, 40));
    me_ready(&mut h, fx("r14w3_prefs_me"));
    open_select(&mut h, "Auto (detected)");
    click_option(&mut h, "English");
    assert_eq!(puts(&h.sent())[0].2, json!({"spoken_language": "en"}));
    h.store
        .json
        .set_write(prefs::ME_WRITE, Some(WriteState::Failed(refused())));
    let s = h.shoot("r18-multimodal-spoken-language-refused");
    assert!(
        flat(&s).contains(&format!(
            "{}{}",
            w("multimodal_not_saved_prefix"),
            refused_sentence()
        )),
        "{s}"
    );
    // A failed read is said.
    h.store.json.set(
        prefs::ME_SLOT,
        Loadable::Failed(ApiError {
            kind: ApiErrorKind::Http(500),
            message: "boom".into(),
            body: None,
            timed_out: false,
        }),
    );
    let s = h.turns(3);
    assert!(
        flat(&s).contains(&w("multimodal_read_failed_prefix")),
        "{s}"
    );
    // An answer without the block: the seam sentence.
    let mut a = fx("r14w3_prefs_me");
    a.as_object_mut().unwrap().remove("spoken_language");
    let s = me_ready(&mut h, a);
    assert!(flat(&s).contains(&w("seam")), "{s}");
}

#[test]
fn multimodal_hovering_the_label_shows_the_gateways_help() {
    let mut h = multimodal((180, 50));
    let s = me_ready(&mut h, fx("r14w3_prefs_me"));
    // The page shows no help line; the tooltip carries it whole.
    assert!(!flat(&s).contains(&help()));
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find(&label()).map(|c| (i, l[..c].chars().count())))
        .expect("label");
    let hover = format!("\x1b[<35;{};{}M", col + 2, row + 1);
    h.key(hover.as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    h.shoot("r18-multimodal-spoken-language-tip");
    assert!(flat(&s).contains(&help()), "{s}");
}
