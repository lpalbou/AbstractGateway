//! R15 (DESIGN-TUI.md §3.15, §6.1): the About page and dialog by mouse.
//! Every link of the kit's About list (Website, Source, Docs, Issues,
//! Feedback, Contact) is a link button: a synthesized click opens its
//! address (the status bar says "opened <url>"), hovering shows the
//! address and its key, the key opens it too. The meta-test enumerates
//! `about::link_actions`: a link without a click test is RED. The words
//! are the kit's (`tests/fixtures/r15_web_wording_about.json`, rebuilt by
//! `scripts/extract_web_wording.py`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::identity::this_app;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, about};
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    about::page(cx, ctx, &t)
}

fn about_payload() -> Value {
    json!({"abstractgateway": "0.13.1", "abstractframework": "0.10.1",
           "packages": {"abstractcore": "2.25.1"}})
}

fn page(mount: Mount) -> r8w4::Harness {
    let mut h = harness((120, 40), mount);
    h.admin();
    h.store.about.set(Loadable::Ready(about_payload()));
    h.turns(3);
    h.sent();
    h
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_about.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn notice(h: &r8w4::Harness) -> String {
    h.store.notice.get_untracked().unwrap_or_default()
}

/// Click the link labelled `label` at the start of its row (the label
/// column), the screen after.
fn click_link(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(&format!("{label}  ")))
        .unwrap_or_else(|| panic!("no {label} link row:\n{screen}"));
    let b = line.find(&format!("{label}  ")).unwrap();
    let x = line[..b].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn covered() -> BTreeSet<&'static str> {
    ["website", "source", "docs", "issues", "feedback", "contact"]
        .into_iter()
        .collect()
}

#[test]
fn every_about_link_has_a_click_test() {
    let offered: BTreeSet<&'static str> = about::link_actions().iter().map(|a| a.id).collect();
    let missing: Vec<_> = offered.difference(&covered()).copied().collect();
    assert!(
        missing.is_empty(),
        "About links without a click test: {missing:?}"
    );
}

#[test]
fn clicking_each_link_opens_its_address() {
    for a in about::link_actions() {
        let mut h = page(Mount::Page(page_view));
        click_link(&mut h, &a.label);
        let want = about::link_target(a.id).unwrap();
        assert_eq!(notice(&h), format!("opened {want}"), "{}", a.label);
    }
    // Contact is the kit's mailto: link.
    assert_eq!(
        about::link_target("contact").unwrap(),
        format!("mailto:{}", this_app().contact_email)
    );
}

#[test]
fn hovering_a_link_shows_its_address_and_key() {
    let mut h = page(Mount::Page(page_view));
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find("Docs  ").map(|c| (i, l[..c].chars().count())))
        .expect("the Docs link");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains(&format!("{}  (d)", this_app().docs)), "{s}");
}

#[test]
fn a_links_key_and_tab_enter_open_it() {
    let mut h = page(Mount::Page(page_view));
    h.key(b"i");
    assert_eq!(notice(&h), format!("opened {}", this_app().issues));
    // Keyboard only: Tab reaches the first link (Website), Enter opens it.
    let mut h = page(Mount::Page(page_view));
    h.key(b"\t");
    h.key(b"\r");
    assert_eq!(notice(&h), format!("opened {}", this_app().website));
}

#[test]
fn the_about_dialog_has_the_links_and_close_and_survives_a_reload() {
    // The whole console; F1 (ESC O P) opens the dialog from any screen.
    let mut h = page(Mount::Root);
    h.key(b"\x1bOP");
    h.store.about.set(Loadable::Ready(about_payload()));
    let s = h.turns(2);
    assert!(s.contains("About AbstractGateway"), "dialog title:\n{s}");
    assert!(s.contains("AbstractGateway   0.13.1"), "{s}");
    // A link inside the dialog opens too.
    let s2 = h.turns(1);
    let (y, line) = s2
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains("Feedback  "))
        .last()
        .expect("Feedback in the dialog");
    let b = line.rfind("Feedback  ").unwrap();
    let x = line[..b].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert_eq!(notice(&h), format!("opened {}", this_app().feedback));
    // GET /about answers again: the dialog stays, with the new version.
    h.store.about.set(Loadable::Ready(
        json!({"abstractgateway": "0.13.2", "abstractframework": "0.10.1"}),
    ));
    let s = h.turns(3);
    assert!(s.contains("About AbstractGateway"), "survived:\n{s}");
    assert!(s.contains("0.13.2"), "{s}");
    // Close by mouse.
    let s = h.click_text(" Close ");
    assert!(!s.contains("About AbstractGateway"), "closed:\n{s}");
}

#[test]
fn the_about_words_are_the_kits() {
    let fx = fixture();
    let id = this_app();
    let field = |f: &str| -> String {
        match f {
            "website" => id.website.clone(),
            "repo" => id.repo.clone(),
            "docs" => id.docs.clone(),
            "issues" => id.issues.clone(),
            "feedback" => id.feedback.clone(),
            "contact_email" => id.contact_email.clone(),
            other => panic!("the kit's About link title reads {other:?}; map it here"),
        }
    };
    let links = fx["links"].as_array().expect("links");
    let ours = about::link_actions();
    assert_eq!(links.len(), ours.len());
    for (web, a) in links.iter().zip(&ours) {
        assert_eq!(web["id"], a.id);
        assert_eq!(web["label"].as_str(), Some(a.label.as_str()));
        assert_eq!(
            a.tooltip.as_deref(),
            Some(field(web["title"].as_str().unwrap()).as_str()),
            "{}",
            a.id
        );
    }
    assert_eq!(fx["dialog"].as_str(), Some(about::ABOUT_TITLE));
    let facts = about::version_facts(false, &Loadable::NotAsked);
    let labels: Vec<&str> = facts.iter().map(|(k, _)| k.as_str()).collect();
    let web: Vec<&str> = fx["version_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(labels, web);
}
