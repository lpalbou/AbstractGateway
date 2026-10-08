//! R16.1 on the round-15 console: Accounts → Preferences → TIME ZONE = the web
//! modal's kit AfTimeZonePicker row (R16.1 API — FINAL (4)). Label and help
//! from the gateway's `time_zone` block, a Combobox whose first option is
//! "Gateway default (<zone>)" (= null), then ONLY the served IANA names,
//! typed to filter; a commit is ONE PUT `{"time_zone": <zone> | null}` at
//! once (no Save), "Saved." / "Not saved. <message>"; an answer without the
//! block shows the web's seam sentence (never a hidden row). Hermetic over
//! answers recorded from a scratch gateway on W1's branch
//! (tests/fixtures/r16w2_tz_*.json; r14w3_prefs_alice_set carries the
//! recorded block). Mouse first (CONVERSION-PATTERN: a click for every
//! control + the meta-test), one keyboard path, one hover. Captures land in
//! R8W4_SHOTS_DIR.

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::account_preferences as prefs;
use abstractgateway_console::ui::{self, users};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};

/// The harness sizes plus a wide terminal (captures at all three).
const SIZES3: [(i32, i32); 3] = [SIZES[0], SIZES[1], (180, 50)];
use serde_json::{json, Value};

const SLOT: &str = "prefs.default.alice";
const WK: &str = "prefs.default.alice.write";
const PATH: &str = "/accounts/alice/preferences";

fn fx(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
}

fn words() -> Value {
    fx("r15_web_wording_preferences_time_zone")
}

fn w(k: &str) -> String {
    words()[k].as_str().expect(k).to_string()
}

fn users_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
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

fn puts(cmds: &[Cmd]) -> Vec<(String, String, Value)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                method, path, body, ..
            }) => Some((method.clone(), path.clone(), body.clone())),
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

/// Click the Combobox trigger on the "Time zone" row (the current label).
fn open_picker(h: &mut r8w4::Harness, current: &str) -> String {
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            let lab = l.find("Time zone")?;
            let c = l[lab..].find(current)? + lab;
            Some((i, l[..c].chars().count()))
        })
        .unwrap_or_else(|| panic!("no Time zone row showing {current:?}:\n{s}"));
    click_at(h, row, col + 1);
    h.turns(2)
}

/// Click a popup option: the popup opens over the trigger (the Combobox
/// mounts its editor on the trigger row), so an option line's text at the
/// trigger's column, trimmed, IS `label` ("Europe/Paris" never hits
/// "Gateway default (Europe/Paris)").
fn click_option(h: &mut r8w4::Harness, label: &str) -> String {
    let s = h.turns(1);
    let lines: Vec<Vec<char>> = s.lines().map(|l| l.chars().collect()).collect();
    let x0 = lines
        .iter()
        .find_map(|l| {
            let t: String = l.iter().collect();
            t.contains("Time zone")
                .then(|| l.iter().position(|c| *c == '▐'))
                .flatten()
        })
        .unwrap_or_else(|| panic!("no Time zone trigger:\n{s}"));
    let (row, col) = lines
        .iter()
        .enumerate()
        .find_map(|(i, l)| {
            let rest: String = l.iter().skip(x0 + 1).collect();
            let lead = rest.len() - rest.trim_start_matches([' ', '▸', '●', '✓']).len();
            let body = rest.trim_start_matches([' ', '▸', '●', '✓']);
            let lone = body.starts_with(label)
                && !body[label.len()..]
                    .starts_with(|c: char| c.is_alphanumeric() || c == '/' || c == ')');
            let rest_chars = rest[..lead].chars().count();
            lone.then_some((i, x0 + 1 + rest_chars))
        })
        .unwrap_or_else(|| panic!("option {label:?} not in the open list:\n{s}"));
    click_at(h, row, col + 1);
    h.turns(2)
}

fn refused() -> ApiError {
    let body = fx("r16w2_tz_refused");
    ApiError {
        kind: ApiErrorKind::Http(400),
        message: body["detail"].to_string(),
        body: Some(body["detail"].clone()),
        timed_out: false,
    }
}

/// Every option class the row offers, by what a pick sends: "null" for the
/// gateway default, "zone" for a served name. Derived from `options()` over
/// the recorded answers — a new class (say a "no longer valid" entry) is a
/// new key the click tests must cover.
fn offered_classes(answer: &Value) -> BTreeSet<&'static str> {
    let tz = prefs::parse(answer).unwrap().time_zone.unwrap();
    tz.options()
        .iter()
        .map(|(v, _)| if v.is_none() { "null" } else { "zone" })
        .collect()
}

/// The classes the click tests below drive by mouse.
const CLICKED: &[&str] = &["null", "zone"];

#[test]
fn every_offered_time_zone_option_has_a_click_test() {
    let mut offered = BTreeSet::new();
    for a in [
        fx("r14w3_prefs_alice_set"),
        fx("r16w2_tz_put_paris"),
        fx("r16w2_tz_put_default"),
    ] {
        offered.extend(offered_classes(&a));
        // The list IS the gateway default then the served names, nothing else.
        let tz = prefs::parse(&a).unwrap().time_zone.unwrap();
        let served: Vec<String> = a["time_zone"]["choices"]
            .as_array()
            .unwrap()
            .iter()
            .map(|z| z.as_str().unwrap().to_string())
            .collect();
        let labels: Vec<String> = tz.options().into_iter().map(|(_, l)| l).collect();
        assert_eq!(
            labels[0],
            w("gateway_default").replace("{time_zone}", "Europe/Paris")
        );
        assert_eq!(labels[1..], served[..]);
    }
    let clicked: BTreeSet<&str> = CLICKED.iter().copied().collect();
    assert_eq!(
        offered, clicked,
        "an offered option class has no click test"
    );
}

#[test]
fn the_row_reads_like_the_web() {
    assert_eq!(prefs::TZ_DEFAULT, w("gateway_default"));
    assert_eq!(prefs::TZ_SEARCH, w("search"));
    assert_eq!(prefs::TZ_SEAM, w("seam"));
    assert_eq!(prefs::SAVED, w("saved"));
    let e = refused();
    assert!(prefs::refusal(&e).starts_with(&w("not_saved_prefix")));
    for size in SIZES3 {
        let mut h = modal(size, fx("r14w3_prefs_alice_set"));
        let s = h.shoot("r16w2-r15-preferences-time-zone");
        let tz = &fx("r14w3_prefs_alice_set")["time_zone"];
        assert!(
            s.lines().any(|l| l.contains(tz["label"].as_str().unwrap())
                && l.contains("Gateway default (Europe/Paris)")),
            "{s}"
        );
        assert!(flat(&s).contains(tz["help"].as_str().unwrap()), "{s}");
        h.assert_fits();
    }
}

#[test]
fn click_open_filter_pick_a_zone_is_one_put_then_saved() {
    for size in SIZES3 {
        let mut h = modal(size, fx("r14w3_prefs_alice_set"));
        let s = open_picker(&mut h, "Gateway default (Europe/Paris)");
        // The list opens on the gateway default, then the served names.
        let f = flat(&s);
        assert!(f.contains("Africa/Abidjan"), "{s}");
        assert!(puts(&h.sent()).is_empty(), "opening sends nothing");
        let s = h.type_text("paris");
        assert!(!flat(&s).contains("Africa/Abidjan"), "typing filters:\n{s}");
        h.shoot("r16w2-r15-preferences-time-zone-filter");
        click_option(&mut h, "Europe/Paris");
        assert_eq!(
            puts(&h.sent()),
            vec![(
                "PUT".to_string(),
                PATH.to_string(),
                json!({"time_zone": "Europe/Paris"})
            )],
            "{size:?}"
        );
        assert!(flat(&h.turns(1)).contains("Saving…"));
        h.store
            .json
            .set_write(WK, Some(WriteState::Done(fx("r16w2_tz_put_paris"))));
        h.store
            .json
            .set(SLOT, Loadable::Ready(fx("r16w2_tz_put_paris")));
        let s = h.shoot("r16w2-r15-preferences-time-zone-saved");
        assert!(flat(&s).contains(&w("saved")), "{s}");
        h.assert_fits();
    }
}

#[test]
fn click_gateway_default_sends_null_not_its_label() {
    let mut h = modal((120, 40), fx("r16w2_tz_put_paris"));
    open_picker(&mut h, "Europe/Paris");
    h.type_text("gateway");
    click_option(&mut h, "Gateway default (Europe/Paris)");
    assert_eq!(
        puts(&h.sent()),
        vec![(
            "PUT".to_string(),
            PATH.to_string(),
            json!({"time_zone": null})
        )]
    );
    h.store
        .json
        .set_write(WK, Some(WriteState::Done(fx("r16w2_tz_put_default"))));
    assert!(flat(&h.turns(3)).contains("Saved."));
}

#[test]
fn a_refusal_is_not_saved_then_the_gateways_sentence() {
    let mut h = modal((120, 40), fx("r14w3_prefs_alice_set"));
    open_picker(&mut h, "Gateway default (Europe/Paris)");
    h.type_text("tokyo");
    click_option(&mut h, "Asia/Tokyo");
    assert_eq!(puts(&h.sent())[0].2, json!({"time_zone": "Asia/Tokyo"}));
    h.store
        .json
        .set_write(WK, Some(WriteState::Failed(refused())));
    let s = h.shoot("r16w2-r15-preferences-time-zone-refused");
    assert!(
        flat(&s).contains("Not saved. time_zone = 'Mars/Olympus' refused: use an IANA time zone name such as 'Europe/Paris', or null for the gateway default."),
        "{s}"
    );
}

#[test]
fn a_missing_block_is_the_seam_sentence_never_a_hidden_row() {
    let mut a = fx("r14w3_prefs_alice_set");
    a.as_object_mut().unwrap().remove("time_zone");
    let mut h = modal((120, 40), a);
    let s = h.shoot("r16w2-r15-preferences-time-zone-missing");
    let f = flat(&s);
    assert!(f.contains(&w("seam")), "{s}");
    // The app rows still render: the error sits in the row's place only.
    assert!(f.contains("AbstractCode — chat agent"), "{s}");
}

#[test]
fn without_can_edit_the_zone_shows_and_a_click_sends_nothing() {
    let mut a = fx("r16w2_tz_put_paris");
    a["can_edit"] = json!(false);
    let mut h = modal((120, 40), a);
    let s = h.turns(1);
    assert!(
        s.lines()
            .any(|l| l.contains("Time zone") && l.contains("Europe/Paris")),
        "{s}"
    );
    open_picker(&mut h, "Europe/Paris");
    assert!(!flat(&h.turns(1)).contains("Africa/Abidjan"));
    assert!(puts(&h.sent()).is_empty());
}

#[test]
fn keyboard_tab_to_the_row_type_enter() {
    let mut h = modal((120, 40), fx("r14w3_prefs_alice_set"));
    // Two app Selects (the first autofocused), then the time-zone Combobox.
    h.key(b"\t");
    h.key(b"\t");
    h.key(b"\r");
    h.type_text("Asia/Tok");
    h.key(b"\r");
    assert_eq!(
        puts(&h.sent()),
        vec![(
            "PUT".to_string(),
            PATH.to_string(),
            json!({"time_zone": "Asia/Tokyo"})
        )]
    );
}

#[test]
fn hovering_the_label_shows_the_gateways_help() {
    let mut h = modal((180, 50), fx("r14w3_prefs_alice_set"));
    let help = fx("r14w3_prefs_alice_set")["time_zone"]["help"]
        .as_str()
        .unwrap()
        .to_string();
    // The modal wraps the help under the row; the tooltip shows it whole.
    let whole = |s: &str| s.lines().any(|l| l.contains(&help));
    assert!(!whole(&h.turns(1)));
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find("Time zone").map(|c| (i, l[..c].chars().count())))
        .expect("Time zone label");
    let hover = format!("\x1b[<35;{};{}M", col + 2, row + 1);
    h.key(hover.as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    h.shoot("r16w2-r15-preferences-time-zone-tip");
    assert!(whole(&s), "{s}");
}
