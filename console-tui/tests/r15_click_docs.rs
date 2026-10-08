//! R15 (DESIGN-TUI.md §3.16, §6.1): the docs assistant drawer by mouse.
//! The header's ✦ Docs and F2 open the right-edge drawer; every control —
//! Past conversations, New conversation, each suggestion, Send, Stop, a
//! past conversation's title (open) and Archive (confirmed by mouse) — is
//! clicked through the real input pipeline and asserted on the command it
//! sends or the state it leaves. The meta-test enumerates the drawer's
//! action builders. The words are the kit's and the console's
//! (`tests/fixtures/r15_web_wording_docs.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::docs::{self, Role};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

/// The whole console (the drawer is installed by the root), connected.
fn root() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Root);
    h.admin();
    h.turns(3);
    h.sent();
    h
}

/// Open the drawer from the header's ✦ Docs button (a mouse click).
fn opened() -> r8w4::Harness {
    let mut h = root();
    let s = h.click_text("✦ Docs");
    assert!(docs::is_open(), "the header button opens the drawer:\n{s}");
    h.sent();
    h
}

fn asks(cmds: &[Cmd]) -> Vec<(String, String)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::DocsAsk {
                question,
                session_id,
                ..
            } => Some((question.clone(), session_id.clone())),
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

/// Click the LAST on-screen occurrence of `needle` (the drawer is on the
/// right, above the page).
fn click_last(h: &mut r8w4::Harness, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(needle))
        .last()
        .unwrap_or_else(|| panic!("{needle:?} not on screen:\n{screen}"));
    let at = line.rfind(needle).unwrap();
    let x = line[..at].chars().count() + 2;
    let _ = &line;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the composer's Send (the line holding the question field).
fn click_send(h: &mut r8w4::Harness) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains('▐') && l.contains("Send"))
        .last()
        .unwrap_or_else(|| panic!("no composer Send:\n{screen}"));
    let at = line.rfind("Send").unwrap();
    let x = line[..at].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_docs.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

/// Two past conversations (one with two questions), and runs of another app.
fn history() -> Value {
    json!({"items": [
        {"run_id": "r1", "session_id": "gateway-docs-assistant:aaa", "created_at": "2026-10-08T01:00:00Z", "updated_at": "2026-10-08T01:00:05Z"},
        {"run_id": "r2", "session_id": "gateway-docs-assistant:aaa", "created_at": "2026-10-08T01:01:00Z", "updated_at": "2026-10-08T01:01:09Z"},
        {"run_id": "r3", "session_id": "gateway-docs-assistant:bbb", "created_at": "2026-10-07T09:00:00Z", "updated_at": "2026-10-07T09:00:04Z"},
        {"run_id": "r9", "session_id": "flow-docs-assistant:zzz", "created_at": "2026-10-08T02:00:00Z"}
    ]})
}

fn covered() -> BTreeSet<&'static str> {
    [
        "history", "new", "send", "stop", "open", "archive", "suggest",
    ]
    .into_iter()
    .collect()
}

#[test]
fn every_docs_action_has_a_click_test() {
    let mut offered: BTreeSet<&'static str> = BTreeSet::new();
    for a in docs::head_actions() {
        offered.insert(a.id);
    }
    for busy in [false, true] {
        for a in docs::composer_actions(busy) {
            offered.insert(a.id);
        }
    }
    for a in docs::history_actions("t") {
        offered.insert(a.id);
    }
    offered.insert("suggest");
    let missing: Vec<_> = offered.difference(&covered()).copied().collect();
    assert!(
        missing.is_empty(),
        "docs actions without a click test: {missing:?}"
    );
}

#[test]
fn f2_and_the_header_button_open_the_drawer_and_esc_closes_it() {
    let mut h = root();
    h.key(b"\x1bOQ");
    let s = h.turns(2);
    assert!(docs::is_open(), "{s}");
    assert!(
        s.contains("Docs assistant") && s.contains("Ask anything about AbstractGateway."),
        "{s}"
    );
    h.esc();
    assert!(!docs::is_open());
    let _ = opened();
}

#[test]
fn a_suggestion_click_asks_it() {
    let mut h = opened();
    click_last(&mut h, "Where do workflows come from?");
    let a = asks(&h.sent());
    assert_eq!(a.len(), 1);
    assert_eq!(a[0].0, "Where do workflows come from?");
    assert_eq!(a[0].1, h.store.docs.session_id.get_untracked());
    assert!(a[0].1.starts_with("gateway-docs-assistant:"), "{}", a[0].1);
}

#[test]
fn send_and_stop_by_mouse() {
    let mut h = opened();
    h.type_text("how do I add a provider?");
    click_send(&mut h);
    assert_eq!(asks(&h.sent())[0].0, "how do I add a provider?");
    let s = h.turns(2);
    assert!(s.contains("Answering…"), "busy:\n{s}");
    // Stop: the pending turn says so; a late answer is not shown.
    click_last(&mut h, "Stop");
    let turns = h.store.docs.turns.get_untracked();
    assert_eq!(turns.last().unwrap().role, Role::Notice);
    assert_eq!(turns.last().unwrap().text, docs::STOPPED);
    assert!(!h.store.docs.busy.get_untracked());
    let s = h.turns(2);
    assert!(s.contains("Stopped. Nothing more will be shown"), "{s}");
    // A new question may go at once (a click puts the caret in the field).
    click_last(&mut h, "▐");
    h.type_text("again");
    click_send(&mut h);
    assert_eq!(asks(&h.sent()).len(), 1);
}

#[test]
fn new_conversation_starts_a_new_session() {
    let mut h = opened();
    click_last(&mut h, "Where do workflows come from?");
    let before = h.store.docs.session_id.get_untracked();
    click_last(&mut h, "New conversation");
    assert_ne!(h.store.docs.session_id.get_untracked(), before);
    assert!(h.store.docs.turns.get_untracked().is_empty());
}

#[test]
fn past_conversations_list_open_and_archive_by_mouse() {
    let mut h = opened();
    click_last(&mut h, "Past conversations   New");
    let g = gets(&h.sent());
    assert!(
        g.contains(&(docs::K_HISTORY.to_string(), docs::HISTORY_PATH.to_string())),
        "{g:?}"
    );
    let s = h.turns(2);
    assert!(s.contains("Loading past conversations…"), "{s}");
    h.store
        .json
        .set(docs::K_HISTORY, Loadable::Ready(history()));
    h.turns(2);
    // Each conversation's first question is read for its title.
    let g = gets(&h.sent());
    for r in ["r1", "r3"] {
        assert!(
            g.contains(&(
                format!("{}{r}", docs::K_INPUT),
                format!("/runs/{r}/input_data")
            )),
            "{g:?}"
        );
    }
    h.store.json.set(
        &format!("{}r1", docs::K_INPUT),
        Loadable::Ready(json!({"input_data": {"prompt": "How do I add a provider?"}})),
    );
    h.store.json.set(
        &format!("{}r3", docs::K_INPUT),
        Loadable::Ready(json!({"input_data": {"prompt": "Where do workflows come from?"}})),
    );
    let s = h.turns(3);
    assert!(
        s.contains("How do I add a provider?") && s.contains("· 2 questions"),
        "{s}"
    );
    assert!(
        !s.contains("zzz"),
        "another app's runs are not listed:\n{s}"
    );
    // Open the first: its questions and answers are read, then shown.
    click_last(&mut h, "How do I add a provider?");
    let g = gets(&h.sent());
    assert!(
        g.contains(&(
            format!("{}r2", docs::K_INPUT),
            "/runs/r2/input_data".to_string()
        )),
        "{g:?}"
    );
    assert!(
        g.contains(&(format!("{}r1", docs::K_RUN), "/runs/r1".to_string())),
        "{g:?}"
    );
    h.store.json.set(
        &format!("{}r2", docs::K_INPUT),
        Loadable::Ready(json!({"input_data": {"prompt": "And a local engine?"}})),
    );
    for (r, a) in [("r1", "Open Providers."), ("r2", "Use Local engines.")] {
        h.store.json.set(
            &format!("{}{r}", docs::K_RUN),
            Loadable::Ready(json!({"status": "completed", "output": {"response": a}})),
        );
    }
    let s = h.turns(3);
    assert_eq!(
        h.store.docs.session_id.get_untracked(),
        "gateway-docs-assistant:aaa"
    );
    assert!(
        s.contains("And a local engine?") && s.contains("Use Local engines."),
        "{s}"
    );
    // Archive the other one: confirmed by mouse.
    click_last(&mut h, "Past conversations   New");
    h.store
        .json
        .set(docs::K_HISTORY, Loadable::Ready(history()));
    let s = h.turns(3);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("Where do workflows come from?") && l.contains("⊟"))
        .unwrap_or_else(|| panic!("the bbb row with its Archive glyph:\n{s}"));
    let x = line[..line.find('⊟').unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    let s = h.turns(2);
    assert!(s.contains(docs::ARCHIVE_QUESTION), "{s}");
    click_last(&mut h, " Archive ");
    let sent = h.sent();
    assert!(
        sent.iter().any(
            |c| matches!(c, Cmd::Json(JsonCmd::Send { method, path, .. })
            if method == "POST" && path == "/sessions/gateway-docs-assistant%3Abbb/archive")
        ),
        "{sent:?}"
    );
}

#[test]
fn hover_tips_and_the_keyboard() {
    let mut h = opened();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find("Grounded on").map(|c| (i, l[..c].chars().count())))
        .expect("footer");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains(docs::FOOTER_TIP), "{s}");
    // Keyboard only: the question field holds the caret; Enter asks.
    let mut h = opened();
    h.type_text("where are the logs?\r");
    assert_eq!(asks(&h.sent())[0].0, "where are the logs?");
}

#[test]
fn the_drawer_survives_an_answer_landing() {
    let mut h = opened();
    click_last(&mut h, "How do I give a user a mailbox?");
    h.store.docs.turns.update(|t| {
        let last = t.last_mut().unwrap();
        *last = docs::Turn {
            role: Role::Assistant,
            text: "Open **Accounts**.".into(),
        };
    });
    h.store.docs.busy.set(false);
    let s = h.turns(3);
    assert!(docs::is_open() && s.contains("Accounts"), "{s}");
}

#[test]
fn the_docs_words_are_the_kits() {
    let fx = fixture();
    let k = &fx["kit"];
    let name = fx["name"].as_str().unwrap();
    assert_eq!(docs::PLACEHOLDER, fx["placeholder"]);
    let sugg: Vec<&str> = fx["suggestions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(docs::SUGGESTIONS.to_vec(), sugg);
    assert_eq!(docs::TITLE, k["title"]);
    let head = docs::head_actions();
    assert_eq!(head[0].label, k["history"].as_str().unwrap());
    assert_eq!(head[0].tooltip.as_deref(), k["history_tip"].as_str());
    assert_eq!(head[1].label, k["new"].as_str().unwrap());
    assert_eq!(head[1].tooltip.as_deref(), k["new_tip"].as_str());
    assert_eq!(
        docs::composer_actions(false)[0].label,
        k["send"].as_str().unwrap()
    );
    let busy = docs::composer_actions(true);
    assert_eq!(busy[0].label, k["busy"].as_str().unwrap());
    assert_eq!(busy[1].label, k["stop"].as_str().unwrap());
    assert_eq!(busy[1].tooltip.as_deref(), k["stop_tip"].as_str());
    assert_eq!(docs::STOPPED, k["stopped"]);
    let fill = |v: &Value| v.as_str().unwrap().replace("{name}", name);
    assert_eq!(docs::EMPTY, fill(&k["empty"]));
    assert_eq!(docs::FOOTER, fill(&k["footer"]));
    assert_eq!(docs::FOOTER_TIP, fill(&k["footer_tip"]));
    assert_eq!(docs::HISTORY_LOADING, k["history_loading"]);
    assert_eq!(docs::HISTORY_ERROR, k["history_error"]);
    assert_eq!(docs::HISTORY_EMPTY, k["history_empty"]);
    assert_eq!(docs::ARCHIVE_QUESTION, k["archive_question"]);
    assert_eq!(docs::UNTITLED, k["untitled"]);
    let h = docs::history_actions("Hello");
    assert_eq!(
        h[1].tooltip.as_deref(),
        Some(
            k["archive_tip"]
                .as_str()
                .unwrap()
                .replace("{title}", "Hello")
                .as_str()
        )
    );
    assert_eq!(k["archive_go"], "Archive");
    assert_eq!(k["archive_keep"], "Cancel");
}

#[test]
fn the_archive_confirm_opens_on_cancel() {
    // Destructive: the focus starts on Cancel, so Enter keeps the conversation.
    let mut h = opened();
    click_last(&mut h, "Past conversations   New");
    h.store
        .json
        .set(docs::K_HISTORY, Loadable::Ready(history()));
    let s = h.turns(3);
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains('⊟'))
        .last()
        .unwrap_or_else(|| panic!("an Archive glyph:\n{s}"));
    let x = line[..line.find('⊟').unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    let s = h.turns(2);
    assert!(s.contains(docs::ARCHIVE_QUESTION), "{s}");
    h.key(b"\r");
    let s = h.turns(2);
    assert!(!s.contains(docs::ARCHIVE_QUESTION), "closed:\n{s}");
    assert!(
        !h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::Json(JsonCmd::Send { .. }))),
        "Enter on the default keeps"
    );
}

#[test]
fn the_status_bar_carries_the_drawers_keys_while_it_is_open() {
    let mut h = root();
    h.ui.screen.set(abstractgateway_console::ui::SCREEN_USERS);
    let s = h.turns(3);
    let bar = |s: &str| s.lines().last().unwrap_or("").to_string();
    assert!(
        bar(&s).contains("Enter Email"),
        "Accounts' keys first:\n{s}"
    );
    h.click_text("✦ Docs");
    let s = h.turns(3);
    assert!(
        bar(&s).contains("n New conversation"),
        "the drawer's keys:\n{}",
        bar(&s)
    );
    assert!(!bar(&s).contains("Enter Email"), "{}", bar(&s));
    h.esc();
    let s = h.turns(3);
    assert!(
        bar(&s).contains("Enter Email"),
        "back to the page's keys:\n{}",
        bar(&s)
    );
}
