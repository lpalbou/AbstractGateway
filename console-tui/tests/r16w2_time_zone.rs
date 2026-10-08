//! R16.1: Accounts → Preferences → TIME ZONE in the TUI console = the web's kit
//! AfTimeZonePicker in words (R16.1 API — FINAL (4)): label and help from the
//! gateway's `time_zone` block, "Gateway default (<zone>)" first, the SERVED IANA
//! names, a list filtered by typing ("Search time zones"), Enter applies at once
//! (one PUT `{"time_zone": <zone> | null}`), "Saved." / "Not saved. <message>",
//! a missing block fails loud. Hermetic over answers recorded from a scratch
//! gateway on W1's branch (tests/fixtures/r16w2_prefs_tz_*.json; the r14w3 prefs
//! fixtures gained the recorded block). Captures land in R8W4_SHOTS_DIR.

mod r8w4;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, users};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

const DOWN: &[u8] = b"\x1b[B";

fn fx(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}.json", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(&path).expect(&path)).expect("json")
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
        .replace("· ·", "·")
}

fn page(size: (i32, i32), accounts: &str) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(users_view));
    h.admin();
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&fx(&format!("r14w3_{accounts}"))).unwrap(),
    ));
    h.turns(3);
    h.sent();
    h
}

fn select(h: &mut r8w4::Harness, id: &str) {
    let idx = h.store.accounts.with_untracked(|d| {
        d.ready()
            .unwrap()
            .iter()
            .filter(|r| !r.archived)
            .position(|r| r.id == id)
    });
    h.ui.account_sel.set(idx.expect(id));
    h.turns(2);
}

fn gets(cmds: &[Cmd]) -> Vec<(String, String)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) => Some((key.clone(), path.clone())),
            _ => None,
        })
        .collect()
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

/// Three terminal widths for the captures.
const WIDTHS: [(i32, i32); 3] = [(80, 24), (120, 40), (180, 50)];
const SLOT: &str = "prefs.default.alice";
const WK: &str = "prefs.default.alice.write";

fn opened(size: (i32, i32), answer: Value) -> r8w4::Harness {
    let mut h = page(size, "accounts_w2");
    select(&mut h, "alice");
    h.key(b"p");
    h.sent();
    h.store.json.set(SLOT, Loadable::Ready(answer));
    h.turns(2);
    // The two app rows, then the time-zone row.
    h.key(DOWN);
    h.key(DOWN);
    h
}

fn typed(h: &mut r8w4::Harness, text: &str) {
    for b in text.bytes() {
        h.key(&[b]);
    }
}

fn failed(body: &Value) -> WriteState {
    WriteState::Failed(ApiError {
        kind: ApiErrorKind::Http(400),
        message: body["detail"].to_string(),
        body: Some(body["detail"].clone()),
        timed_out: false,
    })
}

#[test]
fn the_time_zone_row_reads_like_the_web_and_a_filtered_pick_is_one_put() {
    for size in WIDTHS {
        let mut h = opened(size, fx("r14w3_prefs_alice_set"));
        let s = h.shoot("r16w2-preferences-time-zone");
        let f = flat(&s);
        assert!(
            f.contains("▸ Time zone: Gateway default (Europe/Paris)"),
            "{s}"
        );
        assert!(
            f.contains("Daily, weekly and monthly automations run on this clock."),
            "{s}"
        );
        h.assert_fits();
        // Enter opens the list: the search line, the gateway default first (● = current).
        h.key(b"\r");
        let s = h.shoot("r16w2-preferences-time-zone-open");
        let f = flat(&s);
        assert!(f.contains("Search time zones"), "{s}");
        let gd = f.find("▸● Gateway default (Europe/Paris)").expect(&s);
        let first = f.find("Africa/Abidjan").expect(&s);
        assert!(gd < first, "{s}");
        assert!(f.contains("more"), "{s}");
        h.assert_fits();
        // Typing filters the SERVED names.
        typed(&mut h, "europe/par");
        let s = h.shoot("r16w2-preferences-time-zone-filtered");
        let f = flat(&s);
        assert!(f.contains("europe/par▏"), "{s}");
        // The default's label matches too ("Gateway default (Europe/Paris)"), as in the web.
        assert!(
            f.contains("▸● Gateway default (Europe/Paris)") && f.contains(" Europe/Paris"),
            "{s}"
        );
        assert!(!f.contains("Africa/Abidjan") && !f.contains("Asia/"), "{s}");
        h.assert_fits();
        // ↓ to Europe/Paris, Enter: ONE PUT {time_zone}.
        h.key(DOWN);
        h.key(b"\r");
        assert_eq!(
            puts(&h.sent()),
            vec![(
                "PUT".to_string(),
                "/accounts/alice/preferences".to_string(),
                json!({"time_zone": "Europe/Paris"})
            )]
        );
        h.store
            .json
            .set_write(WK, Some(WriteState::Done(fx("r16w2_prefs_tz_paris"))));
        let s = h.turns(3);
        assert!(flat(&s).contains("Saved."), "{s}");
    }
}

#[test]
fn back_to_the_gateway_default_sends_null_never_the_label() {
    let mut h = opened((120, 40), fx("r16w2_prefs_tz_paris"));
    let s = h.turns(2);
    assert!(flat(&s).contains("Time zone: Europe/Paris"), "{s}");
    h.key(b"\r");
    let s = h.turns(2);
    assert!(
        flat(&s).contains("▸● Europe/Paris"),
        "the list opens on the stored zone: {s}"
    );
    typed(&mut h, "gateway");
    h.key(b"\r");
    assert_eq!(puts(&h.sent())[0].2, json!({"time_zone": null}));
}

#[test]
fn backspace_widens_the_filter_and_no_match_says_so() {
    let mut h = opened((120, 40), fx("r14w3_prefs_alice_set"));
    h.key(b"\r");
    typed(&mut h, "zzzz");
    let s = h.turns(2);
    assert!(flat(&s).contains("No results"), "{s}");
    h.key(b"\r");
    assert!(
        puts(&h.sent()).is_empty(),
        "Enter on no result sends nothing"
    );
    for _ in 0..4 {
        h.key(b"\x7f");
    }
    typed(&mut h, "tokyox");
    h.key(b"\x7f");
    let s = h.turns(2);
    assert!(flat(&s).contains("▸ Asia/Tokyo"), "{s}");
}

#[test]
fn the_list_is_exactly_the_served_choices() {
    let mut v = fx("r14w3_prefs_alice_set");
    v["time_zone"]["choices"] = json!(["Europe/Paris", "Asia/Tokyo"]);
    let mut h = opened((120, 40), v);
    h.key(b"\r");
    let s = h.turns(2);
    let f = flat(&s);
    assert!(
        f.contains("Europe/Paris") && f.contains("Asia/Tokyo"),
        "{s}"
    );
    assert!(
        !f.contains("Africa/Abidjan") && !f.contains("America/"),
        "{s}"
    );
    assert!(!f.contains("more"), "{s}");
}

#[test]
fn a_refusal_is_not_saved_then_the_gateways_message() {
    let mut h = opened((120, 40), fx("r14w3_prefs_alice_set"));
    h.key(b"\r");
    typed(&mut h, "asia/tokyo");
    h.key(b"\r");
    assert_eq!(puts(&h.sent())[0].2, json!({"time_zone": "Asia/Tokyo"}));
    h.store
        .json
        .set_write(WK, Some(failed(&fx("r16w2_prefs_tz_refused"))));
    let s = h.shoot("r16w2-preferences-time-zone-refused");
    assert!(
        flat(&s).contains("Not saved. time_zone = 'Mars/Olympus' refused: use an IANA time zone name such as 'Europe/Paris', or null for the gateway default."),
        "{s}"
    );
}

#[test]
fn a_read_only_answer_does_not_open_the_list() {
    let mut v = fx("r14w3_prefs_alice_set");
    v["can_edit"] = json!(false);
    let mut h = opened((120, 40), v);
    h.key(b"\r");
    let s = h.turns(2);
    assert!(!flat(&s).contains("Search time zones"), "{s}");
}

#[test]
fn an_answer_without_the_time_zone_block_fails_loud() {
    let mut v = fx("r14w3_prefs_alice_set");
    v.as_object_mut().unwrap().remove("time_zone");
    let mut h = page((120, 40), "accounts_w2");
    select(&mut h, "alice");
    h.key(b"p");
    h.sent();
    h.store.json.set(SLOT, Loadable::Ready(v));
    let s = h.turns(3);
    assert!(
        flat(&s).contains("Could not read the preferences of alice: GET /accounts/{id}/preferences answered without a time_zone block (R16.1 preferences seam)."),
        "{s}"
    );
}

#[test]
fn the_recorded_answer_parses() {
    let p = abstractgateway_console::ui::account_preferences::parse(&fx("r16w2_prefs_tz_default"))
        .unwrap();
    assert_eq!(p.time_zone.value, None);
    assert_eq!(
        p.time_zone.default_label(),
        format!("Gateway default ({})", p.time_zone.gateway_default)
    );
    assert_eq!(p.time_zone.options().len(), p.time_zone.choices.len() + 1);
    let _ = gets(&[]);
}
