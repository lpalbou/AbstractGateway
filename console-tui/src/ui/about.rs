//! About — the shared About card (ui-kit `AfAbout`, round 5 R5.4): this
//! console's name and version, the AbstractFramework and AbstractGateway
//! versions from the connected gateway's public `GET /about`, the links
//! (Website, Source, Docs, Issues, Feedback, Contact) and ONE
//! author/licence line. No package list.
//!
//! Two doors, one renderer ([`card_rows`]): the About PAGE at the bottom of
//! the screen list (key `I`, round 7 — the web sidebar's IA) and the About
//! overlay (F1 / `?`, anywhere, the web top bar's About button). A gateway
//! that cannot answer is said in the AbstractGateway row, never a blank.
//!
//! R15: the six links are link buttons (a click or the key opens them; the
//! tooltip is the address, the kit's `title`); the overlay is a FormModal
//! titled as the web top bar's button ("About AbstractGateway").

use abstracttui::prelude::*;
use abstracttui::ui::{Phase, UiEvent};
use serde_json::Value;

use super::util::{line, span, span_bold};
use super::w::action::{button, On};
use super::w::Action;
use super::Ctx;
use crate::identity::{about_card_rows, about_version_facts, this_app};
use crate::store::{ConnPhase, Loadable};
use crate::worker::Cmd;

/// The two version facts for the current state: `connected` = the console
/// holds a verified connection; `about` = the `GET /about` slot.
pub fn version_facts(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    match about {
        Loadable::Ready(v) => about_version_facts(Some(v), None),
        Loadable::Failed(e) => about_version_facts(None, Some(&e.to_string())),
        Loadable::Loading => reading(),
        Loadable::NotAsked if connected => reading(),
        Loadable::NotAsked => about_version_facts(None, None),
    }
}

fn reading() -> Vec<(String, String)> {
    vec![
        (
            "AbstractFramework".into(),
            "reading GET /api/gateway/about…".into(),
        ),
        (
            "AbstractGateway".into(),
            "reading GET /api/gateway/about…".into(),
        ),
    ]
}

/// Every row of the card, in order (label: value, or a bare line).
pub fn card_rows(connected: bool, about: &Loadable<Value>) -> Vec<(String, String)> {
    about_card_rows(&this_app(), &version_facts(connected, about))
}

/// (Re)read `GET /about` when connected; NotAsked otherwise.
fn load(ctx: &Ctx) {
    let store = ctx.store;
    if store.conn.get_untracked().is_connected() {
        store.about.set(Loadable::Loading);
        ctx.send(Cmd::LoadAbout);
    } else {
        store.about.set(Loadable::NotAsked);
    }
}

/// `r` on the About page.
pub fn refresh(ctx: &Ctx) {
    load(ctx);
}

/// The label column's width (the longest label, "AbstractFramework", + 1).
const LABEL_W: usize = 18;

/// The kit's link list (ui-kit `AfAbout`): Website, Source, Docs, Issues,
/// Feedback, Contact — each a link whose tooltip is its address (the kit's
/// `title`), opened in the browser by a click or its key (or said in the
/// status bar with the address when this machine has no display).
pub fn link_actions() -> Vec<Action> {
    let id = this_app();
    vec![
        Action::link("website", "Website")
            .tooltip(id.website.clone())
            .key('w'),
        Action::link("source", "Source")
            .tooltip(id.repo.clone())
            .key('s'),
        Action::link("docs", "Docs")
            .tooltip(id.docs.clone())
            .key('d'),
        Action::link("issues", "Issues")
            .tooltip(id.issues.clone())
            .key('i'),
        Action::link("feedback", "Feedback")
            .tooltip(id.feedback.clone())
            .key('f'),
        Action::link("contact", "Contact")
            .tooltip(id.contact_email.clone())
            .key('c'),
    ]
}

/// Where link `id` goes (the kit's `href`: Contact is a `mailto:`).
pub fn link_target(id: &str) -> Option<String> {
    let a = this_app();
    Some(match id {
        "website" => a.website,
        "source" => a.repo,
        "docs" => a.docs,
        "issues" => a.issues,
        "feedback" => a.feedback,
        "contact" => format!("mailto:{}", a.contact_email),
        _ => return None,
    })
}

/// Open link `id` (the status bar says what happened, with the address).
pub fn open_link(ctx: &Ctx, id: &str) {
    if let Some(url) = link_target(id) {
        let _ = ctx.screens.open_url(&url);
    }
}

/// The page's keys (the links' accelerators). True when handled.
fn handle_key(ctx: &Ctx, key: Key) -> bool {
    let Key::Char(c) = key else { return false };
    match link_actions().into_iter().find(|a| a.key == Some(c)) {
        Some(a) => {
            open_link(ctx, a.id);
            true
        }
        None => false,
    }
}

/// The page's hint pairs (R15: the links' keys, then refresh).
pub fn hints() -> Vec<(&'static str, &'static str)> {
    vec![
        ("w", "Website"),
        ("s", "Source"),
        ("d", "Docs"),
        ("i", "Issues"),
        ("f", "Feedback"),
        ("c", "Contact"),
        ("r", "refresh"),
    ]
}

/// The card (reactive): the name line bold, labels muted, the links as
/// link buttons in the label column, every value WRAPPED to the width (a
/// long link is never cut).
fn card_view(ctx: &Ctx, width: impl Fn() -> i32 + 'static) -> View {
    let ctx = ctx.clone();
    dyn_view_scoped(LayoutStyle::column().gap(0).grow(1.0), move |ccx| {
        let store = ctx.store;
        let t = use_theme(ccx).get().tokens;
        let w = width().max(LABEL_W as i32 + 10) as usize;
        let rows = card_rows(store.conn.get().is_connected(), &store.about.get());
        let links = link_actions();
        let mut views: Vec<View> = Vec::new();
        for (i, (k, v)) in rows.into_iter().enumerate() {
            if k.is_empty() {
                for (j, l) in super::util::wrap_text(&v, w).into_iter().enumerate() {
                    views.push(if i == 0 && j == 0 {
                        line(vec![span_bold(l, t.text)])
                    } else {
                        line(vec![span(l, t.text_muted)])
                    });
                }
                continue;
            }
            let ink = if v.starts_with("unavailable") || v == "not connected" {
                t.warn
            } else {
                t.text
            };
            let link = links.iter().find(|a| a.label == k).cloned();
            for (j, l) in super::util::wrap_text(&v, w - LABEL_W)
                .into_iter()
                .enumerate()
            {
                match (&link, j) {
                    (Some(a), 0) => {
                        let c = ctx.clone();
                        let id = a.id;
                        let pad = LABEL_W as i32 - a.width();
                        views.push(
                            Element::new()
                                .style(LayoutStyle::row().height(Dimension::Cells(1)).shrink(0.0))
                                .child(button(ccx, &t, a, On::Page, true, move || {
                                    open_link(&c, id)
                                }))
                                .child(line(vec![span(
                                    format!("{}{l}", " ".repeat(pad.max(1) as usize)),
                                    ink,
                                )]))
                                .build(),
                        );
                    }
                    _ => {
                        let label = if j == 0 {
                            format!("{k:<LABEL_W$}")
                        } else {
                            " ".repeat(LABEL_W)
                        };
                        views.push(line(vec![span(label, t.text_muted), span(l, ink)]));
                    }
                }
            }
        }
        Element::new()
            .style(LayoutStyle::column().gap(0))
            .children(views)
            .build()
    })
}

/// The About page (bottom of the screen list).
pub fn page(cx: Scope, ctx: &Ctx, t: &TokenSet) -> View {
    let store = ctx.store;
    // Read once per connection (and on `r`).
    {
        let ctx_load = ctx.clone();
        cx.effect(move || {
            let connected = store.conn.with(ConnPhase::is_connected);
            if connected
                && store
                    .about
                    .with_untracked(|a| matches!(a, Loadable::NotAsked))
            {
                load(&ctx_load);
            }
        });
    }
    let vp = crate::ui::page_viewport(cx);
    let keys = ctx.clone();
    // The page block's border + padding take 4 cells.
    Block::new()
        .border(BorderKind::Rounded)
        .title("About")
        .fill(t.surface)
        .layout(
            LayoutStyle::column()
                .gap(0)
                .grow(1.0)
                .padding(Edges::hv(1, 0))
                .clip(),
        )
        .child(
            Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                // The page takes the keyboard (its link keys work at once);
                // Tab walks the links.
                .focusable()
                .autofocus()
                .on(Phase::Bubble, move |ectx, ev| {
                    if let UiEvent::Key(k) = ev {
                        if k.mods.0 == 0 && handle_key(&keys, k.key) {
                            ectx.stop_propagation();
                        }
                    }
                })
                .child(card_view(ctx, move || vp.get().w - 4))
                .build(),
        )
        .element(t)
        .build()
}

/// The About dialog's title (the web top bar's button label).
pub const ABOUT_TITLE: &str = "About AbstractGateway";

/// Open the About overlay (F1, anywhere); a connected console (re)reads
/// `GET /about`.
pub fn open(ctx: &Ctx, cx: Scope) {
    load(ctx);
    let c = ctx.clone();
    super::w::FormModal::new(ABOUT_TITLE).size(96, 22).open(
        ctx,
        cx,
        move |mcx, close, _guard, w| {
            let t = use_theme(mcx).get().tokens;
            let close_btn = close.clone();
            let keys = c.clone();
            Element::new()
                .style(LayoutStyle::column().gap(0).grow(1.0))
                .on(Phase::Bubble, move |ectx, ev| {
                    if let UiEvent::Key(k) = ev {
                        if k.mods.0 == 0 && handle_key(&keys, k.key) {
                            ectx.stop_propagation();
                        }
                    }
                })
                .child(card_view(&c, move || w))
                .child(super::w::form::button_row(vec![
                    super::w::action::button_focused(
                        mcx,
                        &t,
                        &Action::label("close", "Close"),
                        On::Raised,
                        move || close_btn(),
                    ),
                ]))
                .build()
        },
    );
}
